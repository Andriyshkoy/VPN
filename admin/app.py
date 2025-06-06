import asyncio
from dataclasses import asdict

from flask import (Flask, abort, jsonify, make_response, redirect,
                   render_template, request, send_file, url_for)

from core.config import settings
from core.db.unit_of_work import uow
from core.services import (BillingService, Config, ConfigService,
                           ServerService, User, UserService)

app = Flask(__name__, template_folder="templates")

server_service = ServerService(uow)
config_service = ConfigService(uow)
user_service = UserService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)


@app.route("/")
def index():
    require_auth()
    return render_template("index.html")


def require_auth() -> None:
    password = settings.admin_password
    if password:
        auth = request.authorization
        if not auth or auth.password != password:
            resp = make_response("", 401)
            resp.headers["WWW-Authenticate"] = 'Basic realm="Admin"'
            abort(resp)


@app.route("/servers", methods=["GET"])
def list_servers():
    require_auth()
    servers = asyncio.run(server_service.list())
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify([asdict(s) for s in servers])
    return render_template("servers.html", servers=servers)


@app.route("/servers", methods=["POST"])
def create_server():
    require_auth()
    data = request.json if request.is_json else request.form
    server = asyncio.run(
        server_service.create(
            name=data["name"],
            ip=data["ip"],
            port=int(data.get("port", 22)),
            host=data["host"],
            location=data["location"],
            api_key=data["api_key"],
            cost=float(data.get("cost", 0)),
        )
    )
    if request.is_json:
        return jsonify(asdict(server))
    return redirect(url_for("list_servers"))


@app.route("/servers/<int:server_id>", methods=["PUT"])
def update_server(server_id: int):
    require_auth()
    data = request.json or {}

    async def _update():
        async with uow() as repos:
            return await repos["servers"].update(server_id, **data)

    srv = asyncio.run(_update())
    if not srv:
        abort(404)
    return jsonify(asdict(ServerService.from_orm(srv)))


@app.route("/servers/<int:server_id>", methods=["DELETE"])
def delete_server(server_id: int):
    require_auth()
    deleted = asyncio.run(server_service.delete(server_id))
    return jsonify({"deleted": deleted})


@app.route("/servers/<int:server_id>/delete", methods=["POST"])
def delete_server_form(server_id: int):
    require_auth()
    asyncio.run(server_service.delete(server_id))
    return redirect(url_for("list_servers"))


@app.route("/configs", methods=["GET"])
def list_configs():
    require_auth()
    configs = asyncio.run(config_service.list_active())
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify([asdict(c) for c in configs])
    return render_template("configs.html", configs=configs)


@app.route("/configs", methods=["POST"])
def create_config():
    require_auth()
    data = request.json if request.is_json else request.form
    cfg = asyncio.run(
        config_service.create_config(
            server_id=data["server_id"],
            owner_id=data["owner_id"],
            name=data["name"],
            display_name=data.get("display_name", data["name"]),
            use_password=bool(data.get("use_password", False)),
        )
    )
    if request.is_json:
        return jsonify(asdict(cfg))
    return redirect(url_for("list_configs"))


@app.route("/configs/<int:config_id>/download", methods=["GET"])
def download_config(config_id: int):
    require_auth()
    content = asyncio.run(config_service.download_config(config_id))
    path = f"/tmp/config_{config_id}.ovpn"
    with open(path, "wb") as f:
        f.write(content)
    return send_file(path, as_attachment=True)


@app.route("/configs/<int:config_id>", methods=["DELETE"])
def delete_config(config_id: int):
    require_auth()
    asyncio.run(config_service.revoke_config(config_id))
    return jsonify({"deleted": True})


@app.route("/configs/<int:config_id>/delete", methods=["POST"])
def delete_config_form(config_id: int):
    require_auth()
    asyncio.run(config_service.revoke_config(config_id))
    return redirect(url_for("list_configs"))


@app.route("/users", methods=["GET"])
def list_users():
    require_auth()
    users = asyncio.run(user_service.list())
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify([asdict(u) for u in users])
    return render_template("users.html", users=users)


@app.route("/users/<int:user_id>", methods=["GET"])
def view_user(user_id: int):
    require_auth()

    async def _get():
        async with uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                return None, []
            configs = await repos["configs"].list(owner_id=user_id)
            return user, configs

    user_obj, configs = asyncio.run(_get())
    if not user_obj:
        abort(404)
    user = User.from_orm(user_obj)
    cfgs = [Config.from_orm(c) for c in configs]
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify({"user": asdict(user), "configs": [asdict(c) for c in cfgs]})
    return render_template("user_detail.html", user=user, configs=cfgs)


@app.route("/users/<int:user_id>/topup", methods=["POST"])
def top_up(user_id: int):
    require_auth()
    data = request.json if request.is_json else request.form
    amount = float(data.get("amount", 0))
    user = asyncio.run(billing_service.top_up(user_id, amount))
    if request.is_json:
        return jsonify(asdict(user))
    return redirect(url_for("list_configs"))


@app.route("/users/topup", methods=["POST"])
def top_up_form():
    require_auth()
    user_id = int(request.form["user_id"])
    amount = float(request.form["amount"])
    asyncio.run(billing_service.top_up(user_id, amount))
    return redirect(url_for("list_configs"))


if __name__ == "__main__":
    app.run()
