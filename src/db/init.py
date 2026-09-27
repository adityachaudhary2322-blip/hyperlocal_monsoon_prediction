"""Create data/monsoon.db and seed it.

Run:  python -m src.db.init [--reset]

Seeds:
  * every pilot sub-district unit from data/processed/units.gpkg
  * one admin (password from ADMIN_PASSWORD, or generated and printed once)
  * one officer per pilot state, each scoped to that state only
  * 20 fake subscribers spread across the 8 pilot districts, all consented

Passwords are printed ONCE, here, and only their bcrypt hashes are stored. They are
fake demo accounts; the phone numbers are in a reserved test range and belong to
nobody.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string


import pandas as pd

from src.common import ROOT, UNITS_GPKG, load_config, pilot_districts
from src.db.models import AuditLog, Subscriber, Unit, User
from src.db.session import (DB_PATH, create_all, describe, drop_all,
                            is_postgres, session_scope)
from src.report import DataError, summarize
from src.runtime import env

# Committed, GDAL-free unit source; see scripts/build_map_assets.py.
PILOT_ASSET = ROOT / "app" / "assets" / "pilot_units.geojson"

# Fake, non-routable numbers. 99999xxxxx is not an allocated Indian mobile range,
# so nothing here can ever reach a real handset even if mock mode were disabled.
FAKE_PHONE_PREFIX = "+9199999"

FIRST_NAMES = ["Ramesh", "Sita", "Arjun", "Kavita", "Mohan", "Anita", "Suresh",
               "Lakshmi", "Vijay", "Meena", "Prakash", "Radha", "Ganesh", "Sunita",
               "Dinesh", "Asha", "Rakesh", "Geeta", "Manoj", "Pushpa"]
SURNAMES = ["Yadav", "Patil", "Kumar", "Devi", "Singh", "Sharma", "Jadhav",
            "Verma", "Rathore", "Chauhan"]


def generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_password(password: str) -> str:
    import bcrypt

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def load_pilot_units():
    """The 30 pilot sub-districts, as a plain DataFrame.

    Prefers `app/assets/pilot_units.geojson` - it is committed, carries precomputed
    centroids, and needs only pandas, so seeding works on a Hugging Face Space where
    `data/processed/units.gpkg` does not exist and GDAL is not installed. The GeoPackage
    remains the fallback for a checkout that has run the Phase 1 pipeline but not
    `scripts/build_map_assets.py`.
    """
    if PILOT_ASSET.is_file():
        payload = json.loads(PILOT_ASSET.read_text(encoding="utf-8"))
        frame = pd.DataFrame([f["properties"] for f in payload["features"]])
        source = PILOT_ASSET
    elif UNITS_GPKG.is_file():
        import geopandas as gpd

        units = gpd.read_file(UNITS_GPKG, layer="units")
        mask = units["state"].isin([])
        for state, districts in pilot_districts().items():
            mask |= (units["state"] == state) & units["district"].isin(districts)
        frame = units[mask].copy()
        centroid = frame.to_crs("EPSG:6933").geometry.centroid.to_crs("EPSG:4326")
        frame["centroid_lat"] = centroid.y.to_numpy()
        frame["centroid_lon"] = centroid.x.to_numpy()
        frame = pd.DataFrame(frame.drop(columns="geometry"))
        source = UNITS_GPKG
    else:
        raise DataError(
            f"no unit source found. Expected {PILOT_ASSET} (run "
            f"python scripts/build_map_assets.py) or {UNITS_GPKG} (run "
            "python -m src.build_units)"
        )

    # The asset holds only the pilots already; the filter is kept so both sources
    # go through the same check and a stale asset cannot widen the seed.
    mask = frame["state"].isin([])
    for state, districts in pilot_districts().items():
        mask |= (frame["state"] == state) & frame["district"].isin(districts)
    pilots = frame[mask].copy()
    if pilots.empty:
        raise DataError(
            f"no units in {source.name} matched the pilots in config/project.yaml")
    if "unit_type" not in pilots.columns:
        pilots["unit_type"] = "subdistrict"
    print(f"  units from {source.name}: {len(pilots)}")
    return pilots


def seed_units(session, pilots) -> int:
    session.query(Unit).delete()
    for row in pilots.itertuples():
        session.add(Unit(
            unit_id=row.unit_id, unit_name=row.unit_name, district=row.district,
            state=row.state, zone_id=getattr(row, "zone_id", None),
            unit_type=row.unit_type, centroid_lat=float(row.centroid_lat),
            centroid_lon=float(row.centroid_lon), is_pilot=True,
        ))
    return len(pilots)


def seed_users(session, states: list[str]) -> list[tuple[str, str, str, str]]:
    """Returns (username, password, role, scope) so main() can print them once."""
    session.query(User).delete()
    created = []

    admin_password = env("ADMIN_PASSWORD") or generate_password()
    session.add(User(
        username="admin", name="System Administrator",
        email="admin@example.invalid", password_hash=hash_password(admin_password),
        role="admin", assigned_states="",
    ))
    created.append(("admin", admin_password, "admin", "all states"))

    for state in states:
        # "officer_madhya_pradesh" - predictable, and no spaces to fat-finger.
        username = "officer_" + state.lower().replace(" ", "_")
        password = generate_password()
        session.add(User(
            username=username, name=f"{state} Officer",
            email=f"{username}@example.invalid",
            password_hash=hash_password(password),
            role="officer", assigned_states=state,
        ))
        created.append((username, password, "officer", state))
    return created


def seed_subscribers(session, pilots, count: int = 20) -> int:
    session.query(Subscriber).delete()
    languages = load_config("project")["languages"]

    pairs = sorted({(r.state, r.district) for r in pilots.itertuples()})
    rows = []
    for index in range(count):
        state, district = pairs[index % len(pairs)]
        candidates = pilots[(pilots["state"] == state) &
                            (pilots["district"] == district)]
        unit = candidates.iloc[index % len(candidates)]
        name = f"{FIRST_NAMES[index % len(FIRST_NAMES)]} " \
               f"{SURNAMES[index % len(SURNAMES)]}"
        rows.append(Subscriber(
            name=name,
            phone=f"{FAKE_PHONE_PREFIX}{index:05d}",
            language=languages.get(state, ["en"])[0],
            unit_id=unit["unit_id"], district=district, state=state,
            consent=True, do_not_send=False,
        ))
    session.add_all(rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="empty the database first")
    args = parser.parse_args()

    print("--- db.init ---")
    print(f"  database: {describe()}")

    if args.reset:
        # Deleting the file is the right reset for SQLite and impossible for a
        # managed Postgres, where the tables are dropped through the ORM instead.
        if is_postgres():
            drop_all()
            print("  dropped every table")
        elif DB_PATH.exists():
            DB_PATH.unlink()
            print(f"  removed {DB_PATH}")

    create_all()
    pilots = load_pilot_units()
    states = sorted(pilot_districts())

    with session_scope() as session:
        n_units = seed_units(session, pilots)
        accounts = seed_users(session, states)
        n_subscribers = seed_subscribers(session, pilots)
        session.add(AuditLog(username="system", action="db_init",
                             entity="database", entity_id=describe(),
                             detail=f"{n_units} units, {len(accounts)} users, "
                                    f"{n_subscribers} subscribers"))

    print("\n  LOGINS - shown once, only bcrypt hashes are stored:")
    print(f"    {'username':<28}{'password':<16}{'role':<10}scope")
    for username, password, role, scope in accounts:
        print(f"    {username:<28}{password:<16}{role:<10}{scope}")
    if env("ADMIN_PASSWORD"):
        print("    (admin password came from ADMIN_PASSWORD in .env)")

    summarize(
        "db.init",
        rows=n_units + len(accounts) + n_subscribers,
        files=[DB_PATH] if not is_postgres() else [],
        extra={
            "units": n_units,
            "users": len(accounts),
            "subscribers": f"{n_subscribers} (all consented, fake numbers)",
            "states": ", ".join(states),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
