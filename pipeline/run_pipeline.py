"""
pipeline/run_pipeline.py

Orquestrador principal do pipeline de Ouvidoria Analytics.
Coordena: coleta → classificação → persistência → relatórios.

COMO RODAR (de dentro da pasta bradesco_ouvidoria):
  python -m pipeline.run_pipeline --mode full --pages 10
  python -m pipeline.run_pipeline --mode classify_only
  python -m pipeline.run_pipeline --mode report

MODOS:
  full          — scraping + classificação + persistência + relatórios
  classify_only — classifica dados já coletados (latest csv)
  report        — só relatórios (sem scraping nem classificação)

PAGES:
  --pages 5    → rápido, teste (~50 reclamações)
  --pages 50   → coleta relevante (~500 reclamações)
  --pages 200  → coleta completa (~2000 reclamações)
"""

import sys
import os

# ── Fix de importação: garante que a raiz do projeto está no sys.path ──────
# Isso resolve o ModuleNotFoundError independente de como você roda o arquivo
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
# ───────────────────────────────────────────────────────────────────────────

import argparse
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from scraper.scraper_reclameaqui import scrape_incremental, scrape_historical, reclamacoes_to_records
from classifier.llm_classifier import LLMClassifier
from classifier.validation import gerar_relatorio_auditoria

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pipeline")

# ---------------------------------------------------------------------------
# Paths  (relativos à raiz do projeto, não ao arquivo atual)
# ---------------------------------------------------------------------------
DATA_DIR      = Path(_project_root) / "data"
RAW_DIR       = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
REPORTS_DIR   = DATA_DIR / "reports"

MASTER_FILE   = PROCESSED_DIR / "reclamacoes_master.parquet"
LATEST_CSV    = PROCESSED_DIR / "reclamacoes_latest.csv"
AUDIT_EXCEL   = REPORTS_DIR   / "auditoria_revisao_humana.xlsx"


# ---------------------------------------------------------------------------
# Etapa 1 — Coleta
# ---------------------------------------------------------------------------
def etapa_coleta(max_pages: int = 10, max_per_page: int = 10) -> pd.DataFrame:
    """
    Interpreta --pages como:
      <= 10  → incremental (últimos N dias)
      <= 50  → histórico de 1 mês
      > 50   → histórico de 6 meses
    """
    logger.info("=== ETAPA 1: Coleta (max_pages=%d) ===", max_pages)

    if max_pages <= 10:
        logger.info("Modo: incremental — últimos %d dia(s)", max_pages)
        records = scrape_incremental(days_back=max_pages)
    elif max_pages <= 50:
        logger.info("Modo: histórico 1 mês")
        records = scrape_historical(months=1)
    else:
        logger.info("Modo: histórico 6 meses (pode demorar)")
        records = scrape_historical(months=6)

    if not records:
        logger.warning("Nenhuma reclamação coletada.")
        return pd.DataFrame()

    df = pd.DataFrame(records)

    ts       = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    raw_path = RAW_DIR / f"reclamacoes_{ts}.csv"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(raw_path, index=False, encoding="utf-8-sig")
    logger.info("Raw salvo: %s (%d linhas)", raw_path, len(df))
    return df


# ---------------------------------------------------------------------------
# Etapa 2 — Classificação
# ---------------------------------------------------------------------------
def etapa_classificacao(df: pd.DataFrame, api_key: str) -> pd.DataFrame:
    logger.info("=== ETAPA 2: Classificação IA ===")

    if not api_key:
        logger.warning("api-key não fornecida. Pulando classificação.")
        df["categoria"]        = "Não classificado"
        df["subcategoria"]     = "Não classificado"
        df["area_responsavel"] = "Ouvidoria Corporativa"
        df["urgencia"]         = "Média"
        df["resumo_problema"]  = ""
        df["confianca"]        = 0.0
        df["precisa_revisao"]  = True
        return df

    #Garantir que texto_completo seja string (preencher NaN com descricao_curta)
    df["texto_para_classificar"] = df["texto_completo"].fillna("") + " " + df["descricao_curta"].fillna("")
    df["texto_para_classificar"] = df["texto_para_classificar"].astype(str).str.strip()

    clf = LLMClassifier(api_key=api_key)
    records = df.to_dict("records")
    results = clf.classify_batch(records, texto_field="texto_para_classificar") 

    df_results = pd.DataFrame([r.__dict__ for r in results])
    df_merged  = df.merge(
        df_results.drop(columns=["erro"], errors="ignore"),
        on="id_reclamacao",
        how="left",
    )
    return df_merged


# ---------------------------------------------------------------------------
# Etapa 3 — Persistência incremental
# ---------------------------------------------------------------------------
def etapa_persistencia(df_novo: pd.DataFrame) -> pd.DataFrame:
    logger.info("=== ETAPA 3: Persistência ===")
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # Garantir que a coluna 'prescricao' existe e tem valor padrão ───
    if "prescricao" not in df_novo.columns:
        df_novo["prescricao"] = "Revisar caso e contatar o cliente."
    else:
        df_novo["prescricao"] = df_novo["prescricao"].fillna("Revisar caso e contatar o cliente.")

    if MASTER_FILE.exists():
        df_master = pd.read_parquet(MASTER_FILE)
        
        # ─── Se o master antigo não tem 'prescricao', adiciona com valor padrão ───
        if "prescricao" not in df_master.columns:
            df_master["prescricao"] = "Revisar caso e contatar o cliente."
        
        ids_existentes = set(df_master["id_reclamacao"])
        df_incremental = df_novo[~df_novo["id_reclamacao"].isin(ids_existentes)]
        df_final = pd.concat([df_master, df_incremental], ignore_index=True)
        logger.info("+%d novas, total=%d", len(df_incremental), len(df_final))
    else:
        df_final = df_novo
        logger.info("Master criado com %d registros.", len(df_final))

    df_final.to_parquet(MASTER_FILE, index=False)
    df_novo.to_csv(LATEST_CSV, index=False, encoding="utf-8-sig")
    return df_final


# ---------------------------------------------------------------------------
# Etapa 4 — Relatórios
# ---------------------------------------------------------------------------
def etapa_relatorios(df: pd.DataFrame) -> dict:
    logger.info("=== ETAPA 4: Relatórios ===")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Relatórios Excel (já existentes)
    if "categoria" in df.columns:
        (
            df["categoria"]
            .value_counts()
            .reset_index()
            .rename(columns={"index": "categoria", "categoria": "total"})
            .to_excel(REPORTS_DIR / "distribuicao_categorias.xlsx", index=False)
        )

    if "data_hora" in df.columns:
        if "data_iso" in df.columns:
            df["data_hora_parsed"] = pd.to_datetime(df["data_iso"], errors="coerce")
        else:
            # fallback antigo
            df["data_hora_parsed"] = pd.to_datetime(df["data_hora"], errors="coerce", dayfirst=True)
        df["semana"]           = df["data_hora_parsed"].dt.to_period("W")
        (
            df.groupby(["semana", "categoria"])
            .size()
            .reset_index(name="total")
            .to_excel(REPORTS_DIR / "tendencia_semanal.xlsx", index=False)
        )

    if "confianca" in df.columns:
        gerar_relatorio_auditoria(df, output_path=str(REPORTS_DIR / "auditoria_revisao_humana.xlsx"))

    if "urgencia" in df.columns and "area_responsavel" in df.columns:
        (
            df.groupby(["area_responsavel", "urgencia"])
            .size()
            .unstack(fill_value=0)
            .to_excel(REPORTS_DIR / "urgencias_por_area.xlsx")
        )

    # ── NOVO: Exportar para dashboard (JSON completo) ──
    cols_dashboard = [
        "id_reclamacao", "titulo", "descricao_curta", "resumo_problema",
        "categoria", "subcategoria", "area_responsavel", "urgencia",
        "confianca", "precisa_revisao", "justificativa",
        "status_resposta", "data_hora", "local", "data_scraping",
        "prescricao", "data_iso", "data_br", "turno", "url"
    ]
    # Seleciona apenas colunas que existem no DataFrame
    cols_existentes = [c for c in cols_dashboard if c in df.columns]
    df_dash = df[cols_existentes].copy()

    # Converte datas para string ISO (para serialização JSON)
    if "data_scraping" in df_dash:
        df_dash["data_scraping"] = df_dash["data_scraping"].astype(str)
    if "data_hora" in df_dash:
        df_dash["data_hora"] = df_dash["data_hora"].astype(str)

    # Trata valores nulos
    df_dash = df_dash.fillna("")

    json_path = REPORTS_DIR / "dashboard_data.json"
    df_dash.to_json(json_path, orient="records", date_format="iso", force_ascii=False, indent=2)
    logger.info("Dashboard JSON salvo em %s (%d registros)", json_path, len(df_dash))

    # Estatísticas resumidas (log)
    stats = {
        "total_reclamacoes": len(df),
        "categorias":        int(df["categoria"].nunique()) if "categoria" in df else 0,
        "alta_urgencia":     int((df.get("urgencia", pd.Series()) == "Alta").sum()),
        "para_revisao":      int(df.get("precisa_revisao", pd.Series(False)).sum()),
        "confianca_media":   float(df["confianca"].mean()) if "confianca" in df else 0.0,
    }
    logger.info("Stats: %s", stats)
    return stats


# ---------------------------------------------------------------------------
# Pipeline orquestrador
# ---------------------------------------------------------------------------
def run(mode: str, max_pages: int, per_page: int, api_key: str):
    if mode == "full":
        df = etapa_coleta(max_pages=max_pages, max_per_page=per_page)
        if df.empty:
            return
        df = etapa_classificacao(df, api_key=api_key)
        df = etapa_persistencia(df)
        etapa_relatorios(df)

    elif mode == "classify_only":
        if not LATEST_CSV.exists():
            logger.error("latest CSV não encontrado. Rode --mode full primeiro.")
            return
        df = pd.read_csv(LATEST_CSV)
        df = etapa_classificacao(df, api_key=api_key)
        df = etapa_persistencia(df)
        etapa_relatorios(df)

    elif mode == "report":
        if not MASTER_FILE.exists():
            logger.error("master.parquet não encontrado. Rode --mode full primeiro.")
            return
        df = pd.read_parquet(MASTER_FILE)
        etapa_relatorios(df)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ouvidoria Analytics Pipeline — Bradesco")
    parser.add_argument(
        "--mode", default="full",
        choices=["full", "classify_only", "report"],
        help="full=scraping+classif+relatório | classify_only=só classifica | report=só relatórios"
    )
    parser.add_argument(
        "--pages", type=int, default=10,
        help="Páginas do Reclame Aqui a coletar. 10=~100 reclam., 50=~500, 200=~2000."
    )
    parser.add_argument(
        "--per-page", type=int, default=10,
        help="Reclamações detalhadas por página (máx. disponível no site)"
    )
    parser.add_argument(
        "--api-key", default=os.getenv("OPENAI_API_KEY", ""),
        help="OpenAI API key. Se vazio, classifica mas preenche tudo como 'Não classificado'."
    )
    args = parser.parse_args()

    run(
        mode      = args.mode,
        max_pages = args.pages,
        per_page  = args.per_page,
        api_key   = args.api_key,
    )