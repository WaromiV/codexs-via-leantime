from __future__ import annotations

import os
import bcrypt
import pymysql
from mcp.server.fastmcp import FastMCP


DB_HOST = os.environ.get("DB_HOST", "mariadb")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "leantime")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "leantime_pw")
DB_NAME = os.environ.get("DB_NAME", "leantime")


def get_conn():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


server = FastMCP(
    name="Leantime MCP",
    instructions="Tools to manage Leantime users via direct DB access.",
    host="0.0.0.0",
    port=8000,
    streamable_http_path="/mcp",
    json_response=True,
)


@server.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request):  # type: ignore[reportUnknownParameterType]
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


@server.tool(
    name="leantime_list_users",
    description="List Leantime users (username, role, status, createdOn)",
)
def leantime_list_users():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, role, status, createdOn FROM zp_user ORDER BY id DESC LIMIT 50"
            )
            return cur.fetchall()


@server.tool(
    name="leantime_create_user",
    description="Create a Leantime user with email, password, firstname, lastname, role (default editor=20)",
)
def leantime_create_user(
    email: str, password: str, firstname: str, lastname: str, role: str | int = "20"
):
    role_str = str(role)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM zp_user WHERE username=%s", (email,))
            if cur.fetchone():
                return {"status": "exists", "username": email}

            pw_hash = bcrypt.hashpw(
                password.encode(), bcrypt.gensalt(rounds=10)
            ).decode()
            cur.execute(
                """
                INSERT INTO zp_user (username, password, firstname, lastname, phone, role, status, createdOn, modified)
                VALUES (%s, %s, %s, %s, '', %s, 'A', NOW(), NOW())
                """,
                (email, pw_hash, firstname, lastname, role_str),
            )
            return {"status": "created", "username": email, "role": role_str}


if __name__ == "__main__":
    server.run(transport="streamable-http")
