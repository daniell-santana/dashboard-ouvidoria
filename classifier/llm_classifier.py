"""
classifier/llm_classifier.py

Categorização de reclamações bancárias usando GPT (OpenAI) via few-shot prompting.
Retorna categoria principal, subcategoria, área responsável, urgência e score de confiança.

Boas práticas aplicadas:
- Few-shot prompting com exemplos reais do domínio bancário
- Saída estruturada em JSON com campo de confiança (0-1)
- Validação automática + human-in-the-loop para casos de baixa confiança
- Retry com backoff em falhas de API
- Logging detalhado para auditoria
"""

import json
import pandas as pd
import logging
import time
from dataclasses import dataclass
from typing import Optional

import openai  # pip install openai

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Taxonomia de categorias — alinhada ao organograma do Bradesco
# ---------------------------------------------------------------------------
TAXONOMIA = {
    "Conta Corrente / Poupança": {
        "subcategorias": [
            "Bloqueio indevido de conta",
            "Tarifas cobradas sem autorização",
            "Problemas com PIX",
            "Saldo divergente",
            "Encerramento de conta",
        ],
        "area_responsavel": "Varejo Bancário — VP Negócios e Clientes",
    },
    "Cartão de Crédito / Débito": {
        "subcategorias": [
            "Bloqueio / suspensão inesperada",
            "Cobrança indevida na fatura",
            "Limite reduzido sem aviso",
            "Fraude não reconhecida",
            "Cancelamento de cartão",
        ],
        "area_responsavel": "Varejo Bancário — Cartões",
    },
    "Empréstimo / Financiamento": {
        "subcategorias": [
            "Parcela com valor errado",
            "Portabilidade negada",
            "Cobrança após quitação",
            "Juros abusivos",
            "Negativação indevida",
        ],
        "area_responsavel": "Varejo Bancário — Crédito",
    },
    "Investimentos": {
        "subcategorias": [
            "Resgate bloqueado",
            "Rendimento incorreto",
            "Produto não contratado cobrado",
        ],
        "area_responsavel": "Bradesco Asset Management — Prime/Private",
    },
    "Seguros": {
        "subcategorias": [
            "Sinistro negado",
            "Cobrança de seguro não autorizado",
            "Cancelamento sem solicitação",
        ],
        "area_responsavel": "Bradesco Seguros",
    },
    "Atendimento / Canais Digitais": {
        "subcategorias": [
            "App fora do ar / lento",
            "Internet banking com erro",
            "Atendimento SAC ruim",
            "Agência sem solução",
        ],
        "area_responsavel": "VP Tecnologia e Inovação — Canais Digitais",
    },
    "Outros": {
        "subcategorias": ["Não classificado"],
        "area_responsavel": "Ouvidoria Corporativa",
    },
}

URGENCIAS = ["Alta", "Média", "Baixa"]

# ---------------------------------------------------------------------------
# Few-shot examples  (aumentar com mais exemplos melhora precisão)
# ---------------------------------------------------------------------------
FEW_SHOT_EXAMPLES = [
    {
        "titulo": "Cartão bloqueado sem motivo e sem aviso prévio",
        "texto":  "Meu cartão Bradesco Elo foi bloqueado inesperadamente. "
                  "Tentei usar em uma compra de R$150 e foi recusado. "
                  "Liguei pro SAC mas fiquei 2h na fila sem solução.",
        "resposta": {
            "categoria":          "Cartão de Crédito / Débito",
            "subcategoria":       "Bloqueio / suspensão inesperada",
            "area_responsavel":   "Varejo Bancário — Cartões",
            "urgencia":           "Alta",
            "resumo_problema":    "Cliente com cartão Elo bloqueado sem aviso, impossibilitado de compras.",
            "confianca":          0.95,
            "justificativa":      "Título e texto descrevem claramente bloqueio de cartão sem notificação.",
            "prescricao": "Desbloquear cartão e investigar falha no sistema de segurança",

        }
    },
    {
        "titulo": "Cobrado tarifa de conta que nunca pedi",
        "texto":  "Toda mês aparece uma cobrança de R$29,90 de 'pacote de serviços' "
                  "que eu nunca contratei. Fui na agência três vezes e ninguém resolve. "
                  "Quero o estorno de 6 meses de cobranças.",
        "resposta": {
            "categoria":        "Conta Corrente / Poupança",
            "subcategoria":     "Tarifas cobradas sem autorização",
            "area_responsavel": "Varejo Bancário — VP Negócios e Clientes",
            "urgencia":         "Média",
            "resumo_problema":  "Cobrança mensal indevida de pacote de serviços por 6 meses sem contratação.",
            "confianca":        0.92,
            "justificativa":    "Texto indica cobrança recorrente não autorizada em conta corrente.",
            "prescricao": "Cancelar pacote de serviços e estornar valores dos últimos 6 meses"
        }
    },
    {
        "titulo": "App Bradesco travando na hora de fazer pix",
        "texto":  "Toda vez que tento fazer um PIX pelo aplicativo, o app fecha sozinho. "
                  "Já desinstalei duas vezes, já limpei cache e nada resolve. "
                  "Preciso pagar contas urgentes.",
        "resposta": {
            "categoria":        "Atendimento / Canais Digitais",
            "subcategoria":     "App fora do ar / lento",
            "area_responsavel": "VP Tecnologia e Inovação — Canais Digitais",
            "urgencia":         "Alta",
            "resumo_problema":  "Falha crítica no app impedindo realização de PIX para pagamento de contas.",
            "confianca":        0.91,
            "justificativa":    "Problema técnico de canal digital com impacto direto em pagamentos urgentes.",
            "prescricao": "Corrigir falha no módulo PIX do aplicativo e reembolsar eventuais multas"

        }
    },
]


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------
def _build_system_prompt() -> str:
    cats   = "\n".join(f"- {c}" for c in TAXONOMIA.keys())
    urgstr = ", ".join(URGENCIAS)
    return f"""Você é um especialista em ouvidoria bancária do Bradesco.
Sua tarefa é classificar reclamações de clientes com alta precisão e consistência.

CATEGORIAS DISPONÍVEIS:
{cats}

URGÊNCIAS: {urgstr}
- Alta: impacto financeiro imediato, cliente sem acesso a serviço essencial
- Média: problema resolvível em 1-3 dias úteis
- Baixa: insatisfação sem impacto financeiro imediato

REGRAS:
1. Escolha APENAS uma categoria e subcategoria da taxonomia fornecida.
2. Responda SOMENTE com JSON válido, sem texto extra.
3. Inclua o campo "confianca" (0.0 a 1.0) — seja honesto sobre incerteza.
4. Se confiança < 0.7, indique "precisa_revisao": true.
5. "resumo_problema" em 1 frase objetiva (máx. 15 palavras).
6. "area_responsavel" deve ser exatamente como na taxonomia.
7. "prescricao": descreva, de forma objetiva e acionável, a medida que a área responsável deve executar para solucionar o problema do cliente, seguindo boas práticas de atendimento bancário, compliance e experiência do cliente (máx. 20 palavras). Exemplos: "Realizar contestação da cobrança e efetuar estorno imediato" ou "Atualizar cadastro e reprocessar análise de crédito".

FORMATO DE RESPOSTA (JSON):
{{
  "categoria": "...",
  "subcategoria": "...",
  "area_responsavel": "...",
  "urgencia": "Alta|Média|Baixa",
  "resumo_problema": "...",
  "confianca": 0.0-1.0,
  "precisa_revisao": false,
  "justificativa": "...",
  "prescricao": "Ação concreta para a área responsável"
}}"""


def _build_user_prompt(titulo: str, texto: str) -> str:
    examples_str = ""
    for ex in FEW_SHOT_EXAMPLES:
        examples_str += (
            f"\n---\nTítulo: {ex['titulo']}\nTexto: {ex['texto']}\n"
            f"Resposta: {json.dumps(ex['resposta'], ensure_ascii=False)}\n"
        )

    return f"""EXEMPLOS:{examples_str}
---
CLASSIFIQUE A SEGUINTE RECLAMAÇÃO:
Título: {titulo}
Texto: {texto}

Resposta JSON:"""


# ---------------------------------------------------------------------------
# Resultado da classificação
# ---------------------------------------------------------------------------
@dataclass
class ClassificacaoResult:
    id_reclamacao: str
    categoria: str
    subcategoria: str
    area_responsavel: str
    urgencia: str
    resumo_problema: str
    confianca: float
    precisa_revisao: bool
    justificativa: str
    prescricao: str = ""
    erro: Optional[str] = None


# ---------------------------------------------------------------------------
# Classificador
# ---------------------------------------------------------------------------
class LLMClassifier:
    """
    Classifica reclamações usando GPT via few-shot prompting.

    Usage:
        clf = LLMClassifier(api_key="sk-...")
        resultado = clf.classify("rec_id", "Cartão bloqueado", "Meu cartão foi...")
    """

    CONFIANCA_MINIMA = 0.70   # abaixo disso, precisa_revisao=True

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        max_retries: int = 3,
    ):
        self.client      = openai.OpenAI(api_key=api_key)
        self.model       = model
        self.max_retries = max_retries

    def classify(self, id_reclamacao: str, titulo: str, texto: str) -> ClassificacaoResult:
        """Classifica uma reclamação e retorna ClassificacaoResult."""
        # Garantir que texto seja string, tratar NaN/None
        if texto is None or (isinstance(texto, float) and pd.isna(texto)):
            texto = ""
        texto = str(texto)[:2000]  # agora seguro

        system_prompt = _build_system_prompt()
        user_prompt   = _build_user_prompt(titulo, texto)

        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    temperature=0.0,          # determinismo máximo
                    max_tokens=400,
                    response_format={"type": "json_object"},
                )

                raw = response.choices[0].message.content
                data = json.loads(raw)

                confianca       = float(data.get("confianca", 0.5))
                precisa_revisao = data.get("precisa_revisao", confianca < self.CONFIANCA_MINIMA)

                return ClassificacaoResult(
                    id_reclamacao    = id_reclamacao,
                    categoria        = data.get("categoria",        "Outros"),
                    subcategoria     = data.get("subcategoria",     "Não classificado"),
                    area_responsavel = data.get("area_responsavel", "Ouvidoria Corporativa"),
                    urgencia         = data.get("urgencia",         "Média"),
                    resumo_problema  = data.get("resumo_problema",  ""),
                    confianca        = confianca,
                    precisa_revisao  = precisa_revisao,
                    justificativa    = data.get("justificativa",    ""),
                    prescricao = data.get("prescricao", "Revisar caso e contatar o cliente."),
                )

            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Parse error tentativa %d: %s", attempt + 1, exc)
                time.sleep(2 * (attempt + 1))

            except openai.RateLimitError:
                wait = 20 * (attempt + 1)
                logger.warning("Rate limit — aguardando %ss", wait)
                time.sleep(wait)

            except Exception as exc:
                logger.error("Erro inesperado tentativa %d: %s", attempt + 1, exc)
                time.sleep(5)

        # Falha após todas as tentativas
        return ClassificacaoResult(
            id_reclamacao    = id_reclamacao,
            categoria        = "Outros",
            subcategoria     = "Não classificado",
            area_responsavel = "Ouvidoria Corporativa",
            urgencia         = "Baixa",
            resumo_problema  = "",
            confianca        = 0.0,
            precisa_revisao  = True,
            justificativa    = "",
            erro             = "Falha após todas as tentativas de API",
        )

    def classify_batch(
        self,
        reclamacoes: list[dict],
        id_field: str      = "id_reclamacao",
        titulo_field: str  = "titulo",
        texto_field: str   = "texto_completo",
        delay_between: float = 0.5,
    ) -> list[ClassificacaoResult]:
        """Classifica um batch de reclamações com log de progresso."""
        resultados = []
        total = len(reclamacoes)

        for i, rec in enumerate(reclamacoes, 1):
            logger.info("[%d/%d] Classificando %s", i, total, rec.get(id_field, "?"))
            result = self.classify(
                id_reclamacao = rec.get(id_field, f"rec_{i}"),
                titulo        = rec.get(titulo_field, ""),
                texto         = rec.get(texto_field, rec.get("descricao_curta", "")),
            )
            resultados.append(result)
            time.sleep(delay_between)

        aprovadas     = sum(1 for r in resultados if not r.precisa_revisao)
        necessita_rev = total - aprovadas
        logger.info(
            "Batch concluído: %d/%d aprovadas (confiança≥%.0f%%), %d para revisão.",
            aprovadas, total, self.CONFIANCA_MINIMA * 100, necessita_rev
        )
        return resultados
