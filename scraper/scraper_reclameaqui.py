"""
scraper/scraper_reclameaqui.py

Técnicas para contornar o limite de 50 páginas do Reclame Aqui.

ESTRATÉGIAS IMPLEMENTADAS:
1. Intercepção da API interna GraphQL do Reclame Aqui (requisição direta, sem browser)
2. Filtros de data para varrer períodos específicos (janela deslizante)
3. Modo incremental diário — coleta apenas reclamações novas (primeiras páginas)
4. Rotação de User-Agent e delays adaptativos

OBSERVAÇÃO ÉTICA:
Simular chamadas de API internas pode violar os Termos de Serviço do Reclame Aqui.
Este código é para fins de pesquisa/ouvidoria interna — use com responsabilidade.
A abordagem recomendada para produção é solicitar acesso à API oficial.
"""

import re
import json
import time
import random
import logging
from datetime import datetime, date, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÕES GLOBAIS
# ─────────────────────────────────────────────────────────────

GRAPHQL_URL = "https://iosearch.reclameaqui.com.br/raichu-io-site-search-v1/query/companyComplaints/10"
LIST_API_URL = "https://iosite.reclameaqui.com.br/raichu-io-site-v1/complaint/list/company/{company_id}"
BRADESCO_COMPANY_ID = "bradesco"

GRAPHQL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Origin": "https://www.reclameaqui.com.br",
    "Referer": "https://www.reclameaqui.com.br/empresa/bradesco/lista-reclamacoes/",
    "Content-Type": "application/json",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

BASE_LIST_WITH_DATE = (
    "https://www.reclameaqui.com.br/empresa/bradesco/lista-reclamacoes/"
    "?data_abertura_min={date_from}&data_abertura_max={date_to}"
)

RA_SEARCH_API = "https://iosearch.reclameaqui.com.br/raichu-io-site-search-v1/query/companyComplaints/10"

# ─────────────────────────────────────────────────────────────
# FUNÇÕES AUXILIARES (necessárias para o scraping)
# ─────────────────────────────────────────────────────────────

def _rotated_headers() -> dict:
    """Retorna headers com User-Agent aleatório."""
    h = dict(GRAPHQL_HEADERS)
    h["User-Agent"] = random.choice(USER_AGENTS)
    return h


def _adaptive_sleep(attempt: int = 0):
    """Sleep adaptativo: base + jitter exponencial por tentativa."""
    base = random.uniform(1.5, 3.0)
    backoff = (2 ** attempt) * random.uniform(0.5, 1.5)
    time.sleep(min(base + backoff, 30))


def _get(url: str, session: requests.Session, max_retries: int = 3) -> Optional[BeautifulSoup]:
    """GET com retry e backoff exponencial."""
    logger.info("⏳ Requisitando: %s", url) 
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(1.5, 3.0))
            resp = session.get(url, headers=_rotated_headers(), timeout=20)
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                logger.warning("Rate limit (429) em %s — aguardando %ss", url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:
            logger.warning("Tentativa %d/%d falhou para %s: %s", attempt + 1, max_retries, url, exc)
            time.sleep(5 * (attempt + 1))
    return None


def _extract_id_from_url(url: str) -> str:
    """Extrai o ID único da URL da reclamação (últimos chars após o último _)."""
    match = re.search(r"_([A-Za-z0-9\-]+)/?$", url)
    return match.group(1) if match else url.split("/")[-2]


def parse_complaint_cards(soup: BeautifulSoup) -> list[dict]:
    """Extrai cards da página de listagem."""
    cards = soup.find_all("article", class_=re.compile(r"complaint-listagem"))
    results = []

    for card in cards:
        title_link = card.find("a", {"data-testid": "complaint-listagem-v2-title-link"})
        if not title_link:
            continue

        titulo = title_link.get_text(strip=True)
        href   = title_link.get("href", "")
        url    = "https://www.reclameaqui.com.br" + href if href.startswith("/") else href

        descricao_p = card.find("p")
        descricao_curta = descricao_p.get_text(strip=True) if descricao_p else ""

        status_span = card.find("span", class_=re.compile(r"sc-1pe7b5t-5"))
        status = status_span.get_text(strip=True) if status_span else "Desconhecido"

        tempo_span = card.find("span", class_=re.compile(r"sc-1pe7b5t-6"))
        tempo = tempo_span.get_text(strip=True) if tempo_span else ""

        results.append({
            "titulo": titulo,
            "descricao_curta": descricao_curta,
            "status_resposta": status,
            "tempo_publicacao": tempo,
            "url": url,
        })
    return results


def has_next_page(soup: BeautifulSoup) -> bool:
    """Verifica se existe botão de próxima página habilitado."""
    btn = soup.find("button", {"data-testid": "next-page-navigation-button"})
    if not btn:
        return False
    return btn.get("disabled") is None


def parse_complaint_detail(soup: BeautifulSoup, url: str) -> dict:
    """Extrai texto completo, local, data/hora, turno, id_ra."""
    texto_completo = ""

    # Tenta capturar o texto completo
    el = soup.find("p", id="complaint-description")
    if not el:
        el = soup.find("div", {"data-testid": "complaint-description"})
    if not el:
        el = soup.find("p", class_=re.compile(r"text-slate-700"))
    if el:
        texto_completo = el.get_text(separator="\n", strip=True)

    # Local (cidade/UF)
    local = None
    pin_icon = soup.find("svg", {"class": "lucide lucide-map-pin"})
    if pin_icon:
        parent = pin_icon.find_parent("p") or pin_icon.find_parent("div")
        if parent:
            full_text = parent.get_text(separator=" ", strip=True)
            match = re.search(r"([A-Za-zÀ-ÖØ-öø-ÿ\s]+?)\s*[-–,]\s*([A-Z]{2})", full_text)
            if match:
                local = f"{match.group(1).strip()} - {match.group(2)}"
            else:
                parts = full_text.split()
                if len(parts) >= 2:
                    local = " ".join(parts[-2:])

    # Data/hora e turno
    data_hora_raw = None
    for el in soup.find_all(["span", "p", "time"]):
        txt = el.get_text(strip=True)
        if re.search(r"\d{2}/\d{2}/\d{4}", txt):
            data_hora_raw = txt
            break

    data_iso = None
    data_br = None
    turno = None
    if data_hora_raw:
        data_hora_clean = data_hora_raw.replace("Ã s", "às").replace("Ã s", "às")
        match_data = re.search(r'(\d{2})/(\d{2})/(\d{4})', data_hora_clean)
        if match_data:
            dia, mes, ano = match_data.groups()
            data_br = f"{dia}/{mes}/{ano}"
            try:
                data_obj = datetime.strptime(data_br, "%d/%m/%Y")
                data_iso = data_obj.strftime("%Y-%m-%d")
            except:
                pass
        match_hora = re.search(r'(\d{2}):', data_hora_clean)
        if match_hora:
            hora = int(match_hora.group(1))
            if 6 <= hora < 12:
                turno = "manhã"
            elif 12 <= hora < 18:
                turno = "tarde"
            else:
                turno = "noite"

    # ID do Reclame Aqui
    id_ra = None
    id_label = soup.find("b", string="ID:")
    if id_label:
        parent = id_label.find_parent("p") or id_label.find_parent("div")
        if parent:
            text = parent.get_text()
            match = re.search(r"ID:\s*(\d+)", text)
            if match:
                id_ra = match.group(1)

    return {
        "texto_completo": texto_completo,
        "local": local,
        "data_hora": data_hora_raw,
        "data_iso": data_iso,
        "data_br": data_br,
        "turno": turno,
        "id_ra": id_ra,
    }


# ─────────────────────────────────────────────────────────────
# ESTRATÉGIA 1A: Scraping por janelas de datas (sem import circular)
# ─────────────────────────────────────────────────────────────

def scrape_by_date_windows(
    start_date: date,
    end_date: date,
    window_days: int = 7,
    max_pages_per_window: int = 50,
) -> list[dict]:
    """
    Coleta reclamações quebrando por janelas de datas.
    Cada janela tem no máximo 50 páginas.
    """
    session = requests.Session()
    all_records = []
    seen_ids = set()

    current = start_date
    while current <= end_date:
        window_end = min(current + timedelta(days=window_days - 1), end_date)
        date_from = current.strftime("%d/%m/%Y")
        date_to = window_end.strftime("%d/%m/%Y")

        logger.info("Janela %s → %s", date_from, date_to)

        page = 1
        while page <= max_pages_per_window:
            url = BASE_LIST_WITH_DATE.format(date_from=date_from, date_to=date_to)
            if page > 1:
                url += f"&pagina={page}"

            soup = _get(url, session)
            if soup is None:
                logger.warning("Falha na janela %s pág %d", date_from, page)
                break

            cards = parse_complaint_cards(soup)
            if not cards:
                break

            for card in cards:
                rec_id = _extract_id_from_url(card["url"])
                if rec_id in seen_ids:
                    continue
                seen_ids.add(rec_id)

                detail_soup = _get(card["url"], session)
                detail = parse_complaint_detail(detail_soup, card["url"]) if detail_soup else {}

                all_records.append({
                    "id_reclamacao":   rec_id,
                    "titulo":          card["titulo"],
                    "descricao_curta": card["descricao_curta"],
                    "texto_completo":  detail.get("texto_completo", ""),
                    "status_resposta": card["status_resposta"],
                    "tempo_publicacao":card["tempo_publicacao"],
                    "local":           detail.get("local"),
                    "data_hora":       detail.get("data_hora"),
                    "data_iso":        detail.get("data_iso"),
                    "data_br":         detail.get("data_br"),
                    "turno":           detail.get("turno"),
                    "url":             card["url"],
                    "data_scraping":   datetime.utcnow().isoformat(),
                })

            if not has_next_page(soup):
                break
            page += 1

        logger.info("Janela concluída. Total até agora: %d", len(all_records))
        current = window_end + timedelta(days=1)
        _adaptive_sleep()

    return all_records


# ─────────────────────────────────────────────────────────────
# ESTRATÉGIA 1B: API interna (experimental)
# ─────────────────────────────────────────────────────────────

def fetch_via_internal_api(
    company_id: str = "bradesco",
    offset: int = 0,
    limit: int = 10,
    session: Optional[requests.Session] = None,
) -> Optional[dict]:
    """Tenta buscar reclamações via API interna do Reclame Aqui."""
    if session is None:
        session = requests.Session()

    payload = {
        "company": company_id,
        "offset": offset,
        "limit": limit,
        "sort": "DATE_DESC",
    }

    try:
        _adaptive_sleep()
        resp = session.post(
            RA_SEARCH_API,
            json=payload,
            headers=_rotated_headers(),
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("API retornou status %d", resp.status_code)
        return None
    except Exception as e:
        logger.error("Erro na API interna: %s", e)
        return None


def scrape_via_api(total: int = 1000, batch: int = 10) -> list[dict]:
    """Coleta usando a API interna, evitando o limite de paginação HTML."""
    session = requests.Session()
    records = []
    offset = 0

    while offset < total:
        data = fetch_via_internal_api(offset=offset, limit=batch, session=session)

        if data is None:
            logger.warning("API falhou no offset %d. Tentando novamente em 30s.", offset)
            time.sleep(30)
            data = fetch_via_internal_api(offset=offset, limit=batch, session=session)
            if data is None:
                logger.error("Falha persistente. Interrompendo no offset %d.", offset)
                break

        complaints = (
            data.get("data", {})
                .get("getComplaints", {})
                .get("complaints", [])
            or data.get("complaints", [])
            or []
        )

        if not complaints:
            logger.info("Sem mais dados no offset %d. Total coletado: %d", offset, len(records))
            break

        for c in complaints:
            records.append({
                "id_reclamacao":   c.get("id", ""),
                "titulo":          c.get("title", ""),
                "descricao_curta": c.get("description", "")[:200],
                "texto_completo":  c.get("description", ""),
                "status_resposta": c.get("status", ""),
                "local":           c.get("city", "") + " - " + c.get("state", ""),
                "data_hora":       c.get("createdAt", ""),
                "url":             "https://www.reclameaqui.com.br" + c.get("url", ""),
                "data_scraping":   datetime.utcnow().isoformat(),
            })

        logger.info("Offset %d: +%d reclamações (total: %d)", offset, len(complaints), len(records))
        offset += batch

    return records


# ─────────────────────────────────────────────────────────────
# ESTRATÉGIA 2: Incremental diário
# ─────────────────────────────────────────────────────────────

def scrape_incremental(days_back: int = 1) -> list[dict]:
    """Coleta apenas reclamações recentes (últimos N dias)."""
    today = date.today()
    start = today - timedelta(days=days_back)
    logger.info("Coleta incremental: %s → %s", start, today)
    return scrape_by_date_windows(
        start_date=start,
        end_date=today,
        window_days=1,
        max_pages_per_window=50,
    )


# ─────────────────────────────────────────────────────────────
# ESTRATÉGIA 3: Histórico completo
# ─────────────────────────────────────────────────────────────

def scrape_historical(months: int = 6) -> list[dict]:
    """Coleta histórica completa usando janelas de 7 dias."""
    end = date.today()
    start = end - timedelta(days=30 * months)
    logger.info("Coleta histórica: %s → %s (%d meses)", start, end, months)
    return scrape_by_date_windows(
        start_date=start,
        end_date=end,
        window_days=7,
        max_pages_per_window=50,
    )


# ─────────────────────────────────────────────────────────────
# FUNÇÃO DE COMPATIBILIDADE (para o pipeline)
# ─────────────────────────────────────────────────────────────

def reclamacoes_to_records(reclamacoes):
    """Compatibilidade: retorna o próprio dict se já for dict."""
    if reclamacoes and isinstance(reclamacoes[0], dict):
        return reclamacoes
    return [asdict(r) for r in reclamacoes] if reclamacoes else []


# ─────────────────────────────────────────────────────────────
# MAIN (para execução direta)
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["incremental","historical","api","window"],
                        default="incremental")
    parser.add_argument("--days",   type=int, default=1)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--total",  type=int, default=500, help="Para --strategy api")
    args = parser.parse_args()

    if args.strategy == "incremental":
        records = scrape_incremental(days_back=args.days)
    elif args.strategy == "historical":
        records = scrape_historical(months=args.months)
    elif args.strategy == "api":
        records = scrape_via_api(total=args.total)
    elif args.strategy == "window":
        records = scrape_by_date_windows(
            start_date=date(2025, 11, 1),
            end_date=date(2026, 5, 20),
            window_days=7,
        )

    import pandas as pd
    df = pd.DataFrame(records)
    out = f"data/raw/scrape_{args.strategy}_{date.today()}.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"Salvo: {out} ({len(df)} registros)")