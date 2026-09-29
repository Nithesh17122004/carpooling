"""Production configuration gate: fail-fast checks must reject insecure or
incomplete settings and Debug mode before the app can serve traffic."""

import pytest

from backend.config import ProductionConfig, StagingConfig, validate_runtime


def _valid_prod():
    return {
        "ENV": "production",
        "DEBUG": False,
        "JWT_SECRET": "real_secret_value_1",
        "SECRET_KEY": "real_secret_value_2",
        "PAYMENT_PROVIDER": "razorpay",
        "RAZORPAY_KEY_ID": "rzp_live_key",
        "RAZORPAY_KEY_SECRET": "rzp_live_secret",
        "RAZORPAY_WEBHOOK_SECRET": "whsec_live",
        "COOKIE_SECURE": True,
        "FRONTEND_ORIGINS": ["https://app.example.com"],
        "GOOGLE_CLIENT_ID": "",
        "MONGO_URI": "mongodb://10.0.0.5:27017",
        "MONGO_DB_NAME": "ridemate_prod",
        "REDIS_URL": "redis://redis.internal:6379/0",
    }


def test_production_profile_is_hardened():
    assert ProductionConfig.ENV == "production"
    assert ProductionConfig.DEBUG is False
    assert ProductionConfig.COOKIE_SECURE is True
    assert StagingConfig.ENV == "staging"
    assert StagingConfig.DEBUG is False


def test_validate_accepts_valid_production():
    validate_runtime(_valid_prod())


def test_valid_staging_passes():
    cfg = _valid_prod()
    cfg["ENV"] = "staging"
    validate_runtime(cfg)


def test_development_profile_is_never_gated():
    cfg = _valid_prod()
    cfg["ENV"] = "development"
    cfg["DEBUG"] = True  # debug is fine in development
    cfg["PAYMENT_PROVIDER"] = "demo"
    cfg["JWT_SECRET"] = "dev-demo-secret"
    validate_runtime(cfg)  # must not raise


@pytest.mark.parametrize(
    "mutator, label",
    [
        (lambda c: c.update({"DEBUG": True}), "debug enabled"),
        (lambda c: c.update({"PAYMENT_PROVIDER": "demo"}), "demo payment provider"),
        (lambda c: c.update({"JWT_SECRET": "test-only-secret"}), "placeholder jwt secret"),
        (lambda c: c.update({"SECRET_KEY": "change-me"}), "placeholder secret key"),
        (lambda c: c.update({"FRONTEND_ORIGINS": ["*"]}), "wildcard origin"),
        (lambda c: c.update({"FRONTEND_ORIGINS": ["http://insecure.example.com"]}), "http origin"),
        (lambda c: c.update({"COOKIE_SECURE": False}), "secure cookies disabled"),
        (lambda c: c.update({"RAZORPAY_KEY_ID": ""}), "missing razorpay key"),
        (lambda c: c.update({"RAZORPAY_KEY_SECRET": "changeme"}), "placeholder razorpay secret"),
        (lambda c: c.update({"RAZORPAY_WEBHOOK_SECRET": ""}), "missing webhook secret"),
        (lambda c: c.update({"MONGO_URI": "mongodb://localhost:27017"}), "localhost mongo"),
        (lambda c: c.update({"MONGO_DB_NAME": ""}), "missing mongo db name"),
        (lambda c: c.update({"REDIS_URL": ""}), "missing redis"),
        (lambda c: c.update({"SECRET_KEY": "", "APP_SECRET": "test-only-secret"}), "placeholder app secret"),
    ],
)
def test_validate_rejects_insecure_production(mutator, label):
    cfg = _valid_prod()
    mutator(cfg)
    with pytest.raises(RuntimeError):
        validate_runtime(cfg)


def test_create_app_with_production_config_fails_fast():
    # The test env exports JWT_SECRET=test-only-secret, so a production build
    # must refuse to boot here rather than serve with a placeholder secret.
    from backend.app import create_app

    with pytest.raises(RuntimeError):
        create_app(ProductionConfig)