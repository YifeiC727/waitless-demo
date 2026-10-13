import importlib


def test_core_packages_import():
    for name in ["pipecat", "mcp", "fastapi", "boto3", "httpx", "yaml", "numpy"]:
        assert importlib.import_module(name) is not None


def test_nova_sonic_service_importable():
    from pipecat.services.aws.nova_sonic import llm as nova_llm  # noqa: F401
    assert hasattr(nova_llm, "AWSNovaSonicLLMService")


def test_twilio_serializer_importable():
    from pipecat.serializers.twilio import TwilioFrameSerializer
    assert TwilioFrameSerializer is not None


def test_waitless_package_version():
    import waitless
    assert waitless.__version__ == "0.1.0"
