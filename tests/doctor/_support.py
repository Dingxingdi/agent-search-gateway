from pathlib import Path


def environment_name() -> str:
    return "TEST_" + "LLM_" + "CREDENTIAL"


def write_valid_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    credential_field = "api_" + "key_env"
    env_name = environment_name()
    path.write_text(
        "\n".join(
            (
                "[llm_providers.primary]",
                'protocol = "openai"',
                'api_endpoint = "chat_completions"',
                "[[llm_providers.primary.credentials]]",
                'api_url = "https://llm.example.test"',
                f'{credential_field} = "{env_name}"',
                "",
                "[global_default_llm]",
                'provider = "primary"',
                'model = "test-model"',
                "",
            )
        ),
        encoding="utf-8",
    )
