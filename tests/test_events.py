from kalshi_bot.events import Event, EventBlackout


def test_blackout_window(tmp_path):
    cfg = tmp_path / "events.yaml"
    cfg.write_text(
        'events:\n  - {at: "2026-11-13T13:30:00Z", name: "US CPI", before: 10, after: 20}\n'
        "  - {at: 2026-12-16T19:00:00Z, name: FOMC, before: 15, after: 45}\n"
    )
    bo = EventBlackout.load(cfg)
    cpi = bo.events[0]
    assert cpi.name == "US CPI" and cpi.before == 600 and cpi.after == 1200
    assert bo.reason(cpi.at - 599) == "US CPI"
    assert bo.reason(cpi.at + 1199) == "US CPI"
    assert bo.reason(cpi.at - 601) is None and bo.reason(cpi.at + 1201) is None
    assert [e.name for e in bo.upcoming(cpi.at - 3600, within=86400)] == ["US CPI"]
    assert EventBlackout.load(tmp_path / "missing.yaml").reason(0) is None
    assert Event(100.0, "x", 10, 10).covers(105.0)
