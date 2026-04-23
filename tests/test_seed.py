"""Seed loader round-trip."""

from __future__ import annotations

from evk.seed import load_opportunities, load_students, seed_all


def test_load_students_from_seed_json():
    students = load_students()
    assert len(students) >= 2
    ids = {s.id for s in students}
    assert all(student_id for student_id in ids)


def test_load_opportunities_from_seed_json():
    opps = load_opportunities()
    assert len(opps) >= 10
    # every opportunity has an id + required fields
    for o in opps:
        assert o.id
        assert o.title
        assert o.organization
        assert o.summary
    # every id is unique
    assert len({o.id for o in opps}) == len(opps)


def test_seed_all_writes_to_repos(fake_repos):
    counts = seed_all(repos=fake_repos)
    assert counts["students"] == len(load_students())
    assert counts["opportunities"] == len(load_opportunities())
    # Running twice is idempotent (upserts stay at same count)
    seed_all(repos=fake_repos)
    assert len(fake_repos.students.list_all()) == counts["students"]
    assert len(fake_repos.opportunities.list_all()) == counts["opportunities"]
