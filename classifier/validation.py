"""
classifier/validation.py

Como saber se a LLM está categorizando certo?
Implementa 3 estratégias de validação complementares:

1. Gold set  — amostra anotada manualmente + métricas (F1, acurácia por categoria)
2. Agreement — duas LLMs diferentes concordando = maior confiança
3. Audit loop — relatório de divergências para revisão humana periódica
"""

import json
import logging
import random
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from sklearn.metrics import (          # pip install scikit-learn
    classification_report,
    confusion_matrix,
    cohen_kappa_score,
)

from classifier.llm_classifier import LLMClassifier, ClassificacaoResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Validação por gold set (ground truth anotado)
# ---------------------------------------------------------------------------
@dataclass
class GoldSetItem:
    id_reclamacao:    str
    titulo:           str
    texto:            str
    categoria_true:   str
    subcategoria_true: str
    urgencia_true:    str


def avaliar_gold_set(
    classifier: LLMClassifier,
    gold_set: list[GoldSetItem],
    output_csv: Optional[str] = None,
) -> dict:
    """
    Roda o classificador sobre o gold set e retorna métricas.

    Returns dict com:
    - accuracy_categoria   : % de categorias corretas
    - kappa_categoria      : Cohen's Kappa (concordância além do acaso)
    - report_categoria     : classification_report sklearn (string)
    - confusion_matrix     : pd.DataFrame
    - erros_detalhados     : lista de dicts com os casos errados
    """
    y_true_cat, y_pred_cat = [], []
    erros = []

    for item in gold_set:
        pred = classifier.classify(item.id_reclamacao, item.titulo, item.texto)

        y_true_cat.append(item.categoria_true)
        y_pred_cat.append(pred.categoria)

        if pred.categoria != item.categoria_true:
            erros.append({
                "id":              item.id_reclamacao,
                "titulo":          item.titulo,
                "categoria_true":  item.categoria_true,
                "categoria_pred":  pred.categoria,
                "confianca":       pred.confianca,
                "justificativa":   pred.justificativa,
            })

    accuracy = sum(t == p for t, p in zip(y_true_cat, y_pred_cat)) / len(y_true_cat)
    kappa    = cohen_kappa_score(y_true_cat, y_pred_cat)
    report   = classification_report(y_true_cat, y_pred_cat, zero_division=0)

    labels   = sorted(set(y_true_cat + y_pred_cat))
    cm_df    = pd.DataFrame(
        confusion_matrix(y_true_cat, y_pred_cat, labels=labels),
        index=labels, columns=labels
    )

    resultado = {
        "accuracy_categoria": round(accuracy, 4),
        "kappa_categoria":    round(kappa, 4),
        "report_categoria":   report,
        "confusion_matrix":   cm_df,
        "erros_detalhados":   erros,
        "total_amostras":     len(gold_set),
        "total_erros":        len(erros),
    }

    logger.info(
        "Gold set avaliado: accuracy=%.2f%%, kappa=%.3f, erros=%d/%d",
        accuracy * 100, kappa, len(erros), len(gold_set),
    )

    if output_csv:
        cm_df.to_csv(output_csv)
        logger.info("Confusion matrix salva em %s", output_csv)

    return resultado


def criar_gold_set_a_partir_de_df(
    df: pd.DataFrame,
    n_amostras: int = 100,
    seed: int = 42,
) -> list[GoldSetItem]:
    """
    Amostra aleatória de reclamações para anotação manual.
    Retorna lista de GoldSetItem com campos _true vazios (para preencher).

    Uso típico:
        gold_raw = criar_gold_set_a_partir_de_df(df_reclamacoes)
        # ↓ exportar para planilha e anotar manualmente
        pd.DataFrame([g.__dict__ for g in gold_raw]).to_excel("gold_set_para_anotar.xlsx")
    """
    sample = df.sample(n=min(n_amostras, len(df)), random_state=seed)
    return [
        GoldSetItem(
            id_reclamacao     = row.get("id_reclamacao", str(i)),
            titulo            = row.get("titulo", ""),
            texto             = row.get("texto_completo", row.get("descricao_curta", "")),
            categoria_true    = row.get("categoria_true", ""),       # vazio = para anotar
            subcategoria_true = row.get("subcategoria_true", ""),
            urgencia_true     = row.get("urgencia_true", ""),
        )
        for i, (_, row) in enumerate(sample.iterrows())
    ]


# ---------------------------------------------------------------------------
# 2. Inter-rater agreement (duas LLMs)
# ---------------------------------------------------------------------------
def validar_por_agreement(
    classifier_a: LLMClassifier,
    classifier_b: LLMClassifier,
    reclamacoes: list[dict],
    threshold: float = 0.80,
) -> dict:
    """
    Classifica com dois modelos distintos.
    Reclamações onde ambos concordam são consideradas de alta confiança.

    Returns:
        agreement_rate  : proporção de concordâncias
        concordantes    : lista de reclamações onde A==B
        divergentes     : lista para revisão manual
    """
    concordantes, divergentes = [], []

    for rec in reclamacoes:
        pred_a = classifier_a.classify(
            rec["id_reclamacao"], rec["titulo"], rec.get("texto_completo", "")
        )
        pred_b = classifier_b.classify(
            rec["id_reclamacao"], rec["titulo"], rec.get("texto_completo", "")
        )

        if pred_a.categoria == pred_b.categoria:
            concordantes.append({"rec": rec, "pred": pred_a})
        else:
            divergentes.append({
                "rec":    rec,
                "pred_a": pred_a,
                "pred_b": pred_b,
            })

    total       = len(reclamacoes)
    agree_rate  = len(concordantes) / total if total else 0

    logger.info(
        "Inter-rater agreement: %.2f%% (%d/%d). Divergentes para revisão: %d",
        agree_rate * 100, len(concordantes), total, len(divergentes)
    )

    return {
        "agreement_rate": round(agree_rate, 4),
        "concordantes":   concordantes,
        "divergentes":    divergentes,
    }


# ---------------------------------------------------------------------------
# 3. Audit loop — relatório periódico
# ---------------------------------------------------------------------------
def gerar_relatorio_auditoria(
    df_classificado: pd.DataFrame,
    confianca_min: float = 0.70,
    output_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Gera relatório de itens que precisam de revisão humana:
    - confianca < confianca_min
    - precisa_revisao == True
    - erro != None

    Retorna DataFrame ordenado por confiança (menor primeiro).
    """
    mask = (
        (df_classificado["confianca"] < confianca_min)
        | (df_classificado.get("precisa_revisao", False))
        | df_classificado.get("erro", pd.Series(dtype=str)).notna()
    )
    df_revisao = df_classificado[mask].sort_values("confianca")

    logger.info(
        "Auditoria: %d/%d itens precisam de revisão (confiança < %.0f%%)",
        len(df_revisao), len(df_classificado), confianca_min * 100
    )

    if output_path:
        df_revisao.to_excel(output_path, index=False)
        logger.info("Relatório de auditoria salvo em %s", output_path)

    return df_revisao


# ---------------------------------------------------------------------------
# Interpretação dos scores
# ---------------------------------------------------------------------------
INTERPRETACAO_KAPPA = {
    (0.81, 1.00): "Excelente — produção confiável",
    (0.61, 0.80): "Bom — adequado para relatórios gerenciais",
    (0.41, 0.60): "Moderado — revisar prompts e exemplos",
    (0.21, 0.40): "Fraco — necessário ajuste fino ou mais exemplos",
    (0.00, 0.20): "Ruim — modelo inadequado para este domínio",
}

def interpretar_kappa(kappa: float) -> str:
    for (lo, hi), desc in INTERPRETACAO_KAPPA.items():
        if lo <= kappa <= hi:
            return desc
    return "Fora do intervalo esperado"
