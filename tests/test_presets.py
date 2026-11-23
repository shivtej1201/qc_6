"""V5 speed presets and settings, carried into V6."""
from qc6.presets import PRESETS, engine_options, estimate, max_pages, page_type_label, parse_v5_env


def test_preset_sets_rate_and_concurrency():
    e = engine_options({"preset": "enterprise_150k"}, {})
    assert (e["target_rps"], e["max_rps"], e["source_concurrency"], e["image_concurrency"]) == (16.0, 18.0, 35, 50)


def test_typed_speed_is_a_hard_limit():
    e = engine_options({"preset": "enterprise_150k", "rate_rps": 6}, {})
    assert e["target_rps"] == 6 and e["max_rps"] == 6                 # never rises above what a person typed


def test_untouched_preset_may_rise_to_its_ceiling():
    e = engine_options({"preset": "conservative", "rate_rps": 8}, {})
    assert (e["target_rps"], e["max_rps"]) == (8, 10)


def test_run_option_wins_and_v5_timeouts_are_clamped():
    e = engine_options({"preset": "target", "read_timeout": 500, "max_retries": 9}, {"rate_rps": 3})
    assert e["target_rps"] == 3 and e["read_timeout"] == 120.0 and e["max_retries"] == 5
    assert "read_timeout" not in engine_options({}, {})                # not set -> engine keeps V5's default


def test_max_pages():
    assert max_pages({}, {}) == 300_000
    assert max_pages({"max_pages": 5000}, {}) == 5000
    assert max_pages({"max_pages": 5000}, {"max_pages": 20}) == 20


def test_estimate_matches_v5_request_model():
    e = estimate(10_000, 12)
    assert 15.0 <= e["minutes"] <= 16.5                                # V5 model: 10K at 12 req/s ~ 15.6 min
    assert estimate(150_000, PRESETS["enterprise_150k"]["target_rps"])["minutes"] < 180


def test_v5_env_import():
    got = parse_v5_env("""
# QC V5
MAX_URLS_PER_JOB=150000
READ_TIMEOUT="20"
export MAX_RETRIES=2
REQUESTS_PER_SECOND=100
HTTP_CONCURRENCY=200
WORKER_COUNT=abc
""")
    assert got["settings"] == {"max_pages": 150000, "read_timeout": 20.0, "max_retries": 2, "rate_rps": 100.0}
    assert "HTTP_CONCURRENCY" in got["ignored"] and "WORKER_COUNT" in got["ignored"]


def test_page_type_label():
    assert page_type_label("PDP", "B2C") == "B2C PDP"
    assert page_type_label("CLP", None) == "CLP"
    assert page_type_label("NEEDS_REVIEW", "NEEDS_REVIEW") is None
    assert page_type_label("Ambiguous (Conflicting data-pageType: a, b)", None) is None
    assert page_type_label("PLP", "NEEDS_REVIEW") == "PLP"


def test_speed_picked_at_start_wins_over_the_sites_stored_rate():
    """Start crawling with a speed: the site's stored 12/s (Standard) doesn't hold a big list back."""
    from qc6.presets import BIG_LIST, preset_for
    assert preset_for(10_000) == "target" and preset_for(BIG_LIST) == "enterprise_150k" and preset_for(300_000) == "enterprise_150k"
    site = {"preset": "target", "rate_rps": 12}
    e = engine_options(site, {"preset": "enterprise_150k", "rate_rps": PRESETS["enterprise_150k"]["target_rps"]})
    assert (e["target_rps"], e["max_rps"], e["source_concurrency"]) == (16.0, 18.0, 35)
    assert estimate(10_000, 12)["minutes"] <= 16 and estimate(300_000, 16)["minutes"] < estimate(300_000, 12)["minutes"] * 0.8


def test_a_sites_own_limit_is_offered_first_and_kept():
    from qc6.presets import speeds
    assert speeds(10_000, {"preset": "target", "rate_rps": 12})["speed_default"] == "target"       # untouched
    assert speeds(300_000, {})["speed_default"] == "enterprise_150k"
    s = speeds(300_000, {"preset": "target", "rate_rps": 5})
    assert s["speed_default"] == "site" and s["speeds"][0]["rps"] == 5
