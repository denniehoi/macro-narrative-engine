from datetime import date

import numpy as np
import pandas as pd
import pytest

from engine import lifecycle, metrics, scoring, weighting
from engine.settings import load_settings

CFG = load_settings()


# --- scoring -------------------------------------------------------------------------

def test_ema_equilibrium_is_input_for_any_lambda():
    for hl in (2, 10, 30):
        lam = scoring.ema_lambda(hl)
        r = None
        for _ in range(3000):
            r, _ = scoring.update_r(r if r is not None else 0.0, 3.0, lam)
        assert r == pytest.approx(3.0, rel=1e-6)


def test_missing_data_keeps_score_and_marks_stale():
    r, stale = scoring.update_r(2.5, None, 0.2)
    assert r == 2.5 and stale


def test_volume_z_needs_history_and_detects_spike():
    assert scoring.volume_z(100, [10, 12], 7, 0.25) is None
    hist = [10, 11, 9, 10, 12, 10, 11, 9, 10]
    assert scoring.volume_z(11, hist, 7, 0.25) < 1
    assert scoring.volume_z(200, hist, 7, 0.25) > 3


def test_percentile_rank():
    assert scoring.percentile_rank(5, [1, 2, 3, 4]) == 1.0
    assert scoring.percentile_rank(0, [1, 2, 3, 4]) == 0.0
    assert scoring.percentile_rank(2, [1, 2, 3, 4]) == pytest.approx(0.375)
    assert scoring.percentile_rank(None, [1]) is None


def test_m_and_p_components():
    assert scoring.m_component([2.0, 4.0], 2.0) == 1.0
    assert scoring.m_component([1.0, -1.0], 2.0) == pytest.approx(0.25)
    assert scoring.m_component([None], 2.0) is None
    assert scoring.p_component([0.5], 1.0, 4.0) == 0.0
    assert scoring.p_component([2.5], 1.0, 4.0) == pytest.approx(0.5)
    assert scoring.p_component([9.0], 1.0, 4.0) == 1.0


def test_i_score_geometric_mean_punishes_one_weak_component():
    strong = scoring.i_score(0.9, 0.9, 0.8, 0.2, 0.05)
    no_market = scoring.i_score(1.0, 0.0, 1.0, 0.0, 0.05)
    assert strong > 60
    assert no_market < 50  # alleen media-aandacht haalt de drempel niet
    assert scoring.i_score(0.9, None, 0.8, 0.2, 0.05) is None


def test_gates():
    g = scoring.evaluate_gates(alignment_window=[0.7, 0.65], min_alignment=0.6, horizon_class="6_12m",
                               horizon_exempt=False, investable=None, resolved=False)
    assert g["pass"] and g["investable"] is None
    g = scoring.evaluate_gates(alignment_window=[0.7], min_alignment=0.6, horizon_class="lt_3m",
                               horizon_exempt=False, investable=True, resolved=False)
    assert not g["pass"] and not g["horizon"]
    g = scoring.evaluate_gates(alignment_window=[0.7], min_alignment=0.6, horizon_class="lt_3m",
                               horizon_exempt=True, investable=True, resolved=False)
    assert g["pass"]
    g = scoring.evaluate_gates(alignment_window=[0.7], min_alignment=0.6, horizon_class="gt_12m",
                               horizon_exempt=False, investable=False, resolved=False)
    assert not g["pass"]
    g = scoring.evaluate_gates(alignment_window=[0.4, 0.5], min_alignment=0.6, horizon_class="gt_12m",
                               horizon_exempt=False, investable=True, resolved=False)
    assert not g["thesis"]


# --- lifecycle --------------------------------------------------------------------------

def test_streaks_and_exit():
    t = {"streak_in": 2, "gate_fail_streak": 0}
    lifecycle.update_streaks(t, 65, True, CFG)
    assert t["streak_in"] == 3
    lifecycle.update_streaks(t, 65, False, CFG)
    assert t["streak_in"] == 0 and t["gate_fail_streak"] == 1
    today = date(2026, 11, 4)
    assert lifecycle.should_exit({"gate_fail_streak": 0}, 44, today, CFG)
    assert lifecycle.should_exit({"gate_fail_streak": 0}, 50, today, CFG) is None
    assert lifecycle.should_exit({"gate_fail_streak": 3}, 80, today, CFG)
    protected = {"gate_fail_streak": 0, "protected_until": "2026-11-08"}
    assert lifecycle.should_exit(protected, 10, today, CFG) is None


def test_select_active_respects_slots_margin_and_gate3():
    today = date(2026, 11, 4)
    inc = [{"id": f"a{i}", "_i": 70 + i} for i in range(6)]
    weak = {"id": "c1", "_i": 75}         # verslaat 70 niet met 10%
    strong = {"id": "c2", "_i": 90}       # verslaat wel
    no_basket = {"id": "c3", "_i": 95}    # Stock Search faalt
    shock = {"id": "s1", "_i": 20, "_approved_shock": True}
    entered = set()

    def try_enter(t):
        if t["id"] == "c3":
            return False
        entered.add(t["id"])
        return True

    current, displaced = lifecycle.select_active(inc, [shock, no_basket, strong, weak], CFG, try_enter, today)
    ids = {t["id"] for t in current}
    assert len(current) == 6
    assert "s1" in ids and "c2" in ids and "c1" not in ids and "c3" not in ids
    assert {t["id"] for t in displaced} == {"a0", "a1"}


def test_topic_weights_cap_and_cash():
    w, cash = lifecycle.topic_weights([{"id": "a", "_i": 80}, {"id": "b", "_i": 60}], CFG)
    assert w == {"a": 0.25, "b": 0.25} and cash == pytest.approx(0.5)
    six = [{"id": str(i), "_i": v} for i, v in enumerate([95, 90, 70, 65, 62, 61])]
    w, cash = lifecycle.topic_weights(six, CFG)
    assert sum(w.values()) == pytest.approx(1.0)
    assert max(w.values()) <= 0.25 + 1e-9
    assert cash == pytest.approx(0.0, abs=1e-9)


# --- weighting -------------------------------------------------------------------------

def test_tier_from_exposure():
    assert weighting.tier_from_exposure(70, False, CFG) == "pure_play"
    assert weighting.tier_from_exposure(30, False, CFG) == "pick_and_shovel"
    assert weighting.tier_from_exposure(5, False, CFG) == "second_order"
    assert weighting.tier_from_exposure(None, True, CFG) == "pure_play"


def test_megacap_no_longer_outweighs_pure_play():
    members = [
        {"symbol": "OKLO", "tier": "pure_play", "sigma": 0.85, "pre_revenue": False},
        {"symbol": "SMR", "tier": "pure_play", "sigma": 0.90, "pre_revenue": False},
        {"symbol": "PWR", "tier": "pick_and_shovel", "sigma": 0.32},
        {"symbol": "ETN", "tier": "pick_and_shovel", "sigma": 0.28},
        {"symbol": "MSFT", "tier": "second_order", "sigma": 0.22},
    ]
    w, cash, _ = weighting.basket_weights(members, None, CFG)
    assert sum(w.values()) + cash == pytest.approx(1.0)
    assert w["OKLO"] + w["SMR"] == pytest.approx(0.40, abs=1e-9)
    assert w["MSFT"] == pytest.approx(0.20, abs=1e-9)
    assert w["OKLO"] > w["MSFT"] / 2


def test_cap_and_pre_revenue_cap():
    members = [{"symbol": f"P{i}", "tier": "pure_play", "sigma": 0.5} for i in range(2)]
    members += [{"symbol": "PRE", "tier": "pure_play", "sigma": 0.3, "pre_revenue": True}]
    members += [{"symbol": "Q", "tier": "pick_and_shovel", "sigma": 0.3}]
    w, cash, _ = weighting.basket_weights(members, None, CFG)
    assert max(w.values()) <= 0.30 + 1e-9
    assert w["PRE"] <= 0.10 + 1e-9


def test_correlated_names_share_one_weight():
    names = ["A", "B", "C"]
    corr = pd.DataFrame([[1, 0.95, 0.1], [0.95, 1, 0.1], [0.1, 0.1, 1]], index=names, columns=names)
    members = [{"symbol": s, "tier": "pure_play", "sigma": 0.4} for s in names]
    members.append({"symbol": "D", "tier": "pick_and_shovel", "sigma": 0.4})
    members.append({"symbol": "E", "tier": "pick_and_shovel", "sigma": 0.4})
    w, _, clusters = weighting.basket_weights(members, corr, CFG)
    assert clusters["A"] == clusters["B"] != clusters["C"]
    assert w["A"] + w["B"] == pytest.approx(w["C"], rel=1e-9)


def test_portfolio_caps_and_liquidity():
    topic_w = {"t1": 0.25, "t2": 0.25}
    baskets = {"t1": {"X": 0.30, "Y": 0.70}, "t2": {"X": 0.30, "Z": 0.70}}
    # X zit in twee topics: 0.075 + 0.075 = 0.15 -> cap 10%
    targets, cash = weighting.portfolio_targets(topic_w, baskets, {"Z": 500_000}, CFG)
    assert targets["X"]["weight"] == pytest.approx(0.10)
    # Z: 1% van 500k dagomzet = 5k EUR op 100k portefeuille = 5%
    assert targets["Z"]["weight"] == pytest.approx(0.05)
    # Y: 0.25 x 0.70 = 17,5% -> cap 10%
    assert targets["Y"]["weight"] == pytest.approx(0.10)
    assert cash == pytest.approx(1 - 0.10 - 0.10 - 0.05)


def test_drift_and_rebalance_rules():
    targets = {"A": 0.5, "B": 0.5}
    assert weighting.implied_drift(targets, {"A": 100, "B": 100}, {"A": 100, "B": 100}) == 0
    d = weighting.implied_drift(targets, {"A": 100, "B": 100}, {"A": 130, "B": 100})
    assert d > 0.05
    last = {"last_rebalance_day": "2026-11-02"}
    assert weighting.needs_rebalance(None, date(2026, 11, 4), False, 0, CFG)[0]
    assert not weighting.needs_rebalance(last, date(2026, 11, 4), False, 0.01, CFG)[0]
    assert weighting.needs_rebalance(last, date(2026, 11, 4), False, 0.06, CFG)[0]
    assert weighting.needs_rebalance(last, date(2026, 12, 1), False, 0.0, CFG)[1] == "maandelijkse rebalance"
    assert weighting.needs_rebalance(last, date(2026, 11, 4), True, 0.0, CFG)[0]


# --- metrics ---------------------------------------------------------------------------

def _series(returns, start=100.0):
    idx = pd.bdate_range("2026-01-01", periods=len(returns) + 1)
    return pd.Series(start * np.exp(np.concatenate([[0], np.cumsum(returns)])), index=idx)


def test_metrics_z_move_and_vol():
    rng = np.random.default_rng(0)
    flat = _series(rng.normal(0, 0.01, 200))
    trend = _series(np.concatenate([rng.normal(0, 0.01, 180), np.full(20, 0.01)]))
    assert abs(metrics.z_move(flat, 20)) < 3
    assert metrics.z_move(trend, 20) > 2
    assert metrics.z_move(trend, 20, direction=-1) < -2
    vol = metrics.realized_vol(flat, 60)
    assert 0.10 < vol < 0.22
    assert metrics.z_move(flat.iloc[:30], 20) is None
    rel = metrics.z_move(trend, 20, reference=trend)
    assert rel is None  # excess return nul -> geen spreiding


def test_unknown_exposure_falls_back_to_role():
    assert weighting.tier_from_exposure(None, False, CFG, "pure_play") == "pure_play"
    assert weighting.tier_from_exposure(None, False, CFG, None) == "second_order"
    assert weighting.tier_from_exposure(30, False, CFG, "pure_play") == "pick_and_shovel"  # getal wint van rol


# --- papieren handel ------------------------------------------------------------------

def test_basket_perf_in_eur_with_fx_and_benchmark():
    from engine import paper

    members = [{"symbol": "A", "weight": 0.6, "entry_price": 100, "entry_fx": 0.90},
               {"symbol": "B", "weight": 0.3, "entry_price": 50, "entry_fx": 1.0},
               {"symbol": "C", "weight": 0.1, "entry_price": 10, "entry_fx": 0.90}]  # C heeft geen koers
    perf = paper.basket_perf(members, {"A": 110, "B": 45}, {"A": 0.95, "B": 1.0}, 200.0, 210.0)
    a_eur = (110 * 0.95) / (100 * 0.90) - 1
    assert perf["per_symbol"]["A"]["local"] == pytest.approx(0.10)
    assert perf["per_symbol"]["A"]["eur"] == pytest.approx(a_eur)
    assert perf["return_eur"] == pytest.approx(0.6 * a_eur + 0.3 * -0.10)
    assert perf["bench_eur"] == pytest.approx(0.05)
    assert perf["missing"] == ["C"]


def test_paper_portfolio_rebalance_and_mark():
    from engine import paper

    targets = [{"symbol": "A", "weight": 0.15}, {"symbol": "B", "weight": 0.10}]
    holdings, cash, values = paper.rebalance(100_000, targets, {"A": 100, "B": 20}, {"A": 0.9, "B": 1.0})
    assert values == {"A": 15_000, "B": 10_000} and cash == pytest.approx(75_000)
    assert holdings["A"] == pytest.approx(15_000 / 90)
    nav, vals = paper.mark_to_market(holdings, cash, {"A": 110, "B": None}, {"A": 0.9, "B": 1.0}, values)
    assert vals["B"] == 10_000  # geen koers: laatste waarde
    assert nav == pytest.approx(75_000 + 16_500 + 10_000)
