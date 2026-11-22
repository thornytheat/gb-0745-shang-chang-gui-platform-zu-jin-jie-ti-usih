from app.web import find_run
from flask import Blueprint, jsonify

routes = Blueprint("exit", __name__)


@routes.get("/api/exit/<run_id>")
def inspect_exit(run_id):
    run = find_run(run_id)
    return jsonify({key: run[key] for key in ("退租清算", "清算明细", "清算挂起", "清算汇总")})
