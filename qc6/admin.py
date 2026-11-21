"""Workspace administration from the command line (for proxy / OIDC sign-in, where nobody is created automatically).

    python -m qc6.admin create-workspace "Acme Digital" priya@acme.com "Priya Nair"
    python -m qc6.admin add-member 1 rahul@acme.com "Rahul Mehta" editor
    python -m qc6.admin list
    python -m qc6.admin list-sites
    python -m qc6.admin import-v5-config 1 path/to/qc_v5/.env      # V5 settings (page limit, timeouts, retries) -> site 1
"""
from __future__ import annotations

import asyncio
import sys

from qc6 import services
from qc6.db.migrate import migrate
from qc6.db.pool import close_pool, get_pool


async def main(argv) -> int:
    await migrate()
    pool = await get_pool()
    try:
        if argv[:1] == ["create-workspace"] and len(argv) == 4:
            out = await services.create_workspace(pool, argv[1], argv[2], argv[3])
            print(f"Workspace {out['workspace_id']} created; {argv[2]} is its owner.")
        elif argv[:1] == ["add-member"] and len(argv) in (4, 5):
            role = argv[4] if len(argv) == 5 else "editor"
            if role not in ("owner", "editor", "viewer"):
                print("Role must be owner, editor or viewer.")
                return 2
            mid = await services.add_member(pool, int(argv[1]), argv[2], argv[3], role)
            print(f"{argv[2]} added as {role} (member {mid}).")
        elif argv[:1] == ["import-v5-config"] and len(argv) == 3:
            from qc6.presets import parse_v5_env
            with open(argv[2], encoding="utf-8") as f:
                got = parse_v5_env(f.read())
            st = await pool.fetchval("SELECT settings FROM sites WHERE id = $1", int(argv[1]))
            if st is None:
                print(f"There is no site {argv[1]}. See the site numbers with: python -m qc6.admin list-sites")
                return 2
            await pool.execute("UPDATE sites SET settings = settings || $2::jsonb WHERE id = $1", int(argv[1]), got["settings"])
            await services.log(pool, None, None, "site.import_v5_config", f"site:{argv[1]}", got["settings"])
            for k, v in got["settings"].items():
                print(f"  {k} = {v}")
            if got["ignored"]:
                print("Not used by V6 (left as they were): " + ", ".join(got["ignored"]))
        elif argv[:1] == ["list-sites"]:
            for r in await pool.fetch("SELECT id, name, base_url FROM sites ORDER BY id"):
                print(f"{r['id']:>4}  {r['name']:<30}  {r['base_url']}")
        elif argv[:1] == ["list"]:
            for r in await pool.fetch("SELECT w.id, w.name, m.email, m.role FROM workspaces w LEFT JOIN members m ON m.workspace_id = w.id ORDER BY w.id, m.id"):
                print(f"{r['id']:>4}  {r['name']:<30}  {r['email'] or '':<35}  {r['role'] or ''}")
        else:
            print(__doc__)
            return 2
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
