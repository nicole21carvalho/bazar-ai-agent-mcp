"""Escolha do modelo de linguagem: local (Ollama, gratuito) ou na nuvem (Claude, com chave)."""

from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama

from bazar_agente.config import ConfigAgente


def criar_modelo(config: ConfigAgente) -> BaseChatModel:
    # Temperatura zero: o mesmo pedido, a mesma decisão (e avaliações reproduzíveis).
    match config.provedor:
        case "ollama":
            return ChatOllama(
                model=config.modelo,
                base_url=str(config.ollama_url),
                temperature=0,
                reasoning=False,  # sem "pensar em voz alta": menos latência num modelo pequeno
                num_ctx=8192,
                keep_alive="30m",
            )
        case "anthropic":
            try:
                from langchain_anthropic import ChatAnthropic
            except ImportError as erro:
                raise SystemExit("Para usar o Claude: uv sync --extra anthropic") from erro
            if not config.anthropic_chave.get_secret_value():
                raise SystemExit("Defina BAZAR_AGENTE_ANTHROPIC_CHAVE.")
            claude: BaseChatModel = ChatAnthropic(
                model=config.modelo, api_key=config.anthropic_chave, temperature=0, max_tokens=1024
            )
            return claude
        case outro:
            raise SystemExit(f"Provedor desconhecido: {outro} (use ollama ou anthropic).")
