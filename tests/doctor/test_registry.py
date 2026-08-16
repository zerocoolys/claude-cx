"""注册表的调度契约：崩溃隔离、确定性排序。"""

import pytest

from cx.doctor import registry
from cx.doctor.registry import Finding, run_checks

from .conftest import make_probe


@pytest.fixture
def isolated_registry(monkeypatch):
    """给每个测试一张干净的注册表，不受真实检查影响。"""
    monkeypatch.setattr(registry, "_CHECKS", [])
    return registry


def finding(fid, severity="warn", where=""):
    return Finding(id=fid, severity=severity, title="t", detail="d",
                   where=where, fix="f")


def test_registered_check_runs_and_returns_findings(isolated_registry):
    # Arrange
    @isolated_registry.check
    def _c(probe):
        return [finding("refs.a")]

    # Act
    out = run_checks(make_probe())

    # Assert
    assert [f.id for f in out] == ["refs.a"]


def test_crashing_check_does_not_kill_the_run(isolated_registry):
    # Arrange
    @isolated_registry.check
    def _boom(probe):
        raise RuntimeError("炸了")

    @isolated_registry.check
    def _ok(probe):
        return [finding("refs.a")]

    # Act
    out = run_checks(make_probe())

    # Assert
    ids = [f.id for f in out]
    assert "refs.a" in ids
    assert "internal.check-crashed" in ids
    crashed = next(f for f in out if f.id == "internal.check-crashed")
    assert crashed.severity == "warn"
    assert "_boom" in crashed.where
    assert "炸了" in crashed.detail


def test_output_is_sorted_by_severity_then_id_then_where(isolated_registry):
    # Arrange
    @isolated_registry.check
    def _c(probe):
        return [
            finding("schema.z", "info", where="b"),
            finding("refs.a", "error", where="b"),
            finding("refs.a", "error", where="a"),
            finding("conflicts.m", "warn", where="c"),
        ]

    # Act
    out = run_checks(make_probe())

    # Assert
    assert [(f.id, f.where) for f in out] == [
        ("refs.a", "a"), ("refs.a", "b"), ("conflicts.m", "c"), ("schema.z", "b"),
    ]


def test_registration_order_does_not_affect_output(isolated_registry):
    # Arrange
    def a(probe):
        return [finding("refs.a", "error")]

    def b(probe):
        return [finding("schema.b", "info")]

    isolated_registry.check(a)
    isolated_registry.check(b)
    first = run_checks(make_probe())

    isolated_registry._CHECKS.clear()
    isolated_registry.check(b)
    isolated_registry.check(a)
    second = run_checks(make_probe())

    # Assert
    assert first == second


def test_finding_is_immutable():
    f = finding("refs.a")
    with pytest.raises(Exception):
        f.severity = "error"
