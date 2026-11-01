"""Check six synthetic messages through the banking product with one site deployment.

Run only with an explicitly supplied credential directory. No automatic approvals are
made. Each message gets a fresh synthetic session, so one pending card cannot block the
next scenario. Requests use the fixed deployment profile and the standard LLM client.
"""

import argparse
import json
from fractions import Fraction
from pathlib import Path

from corridor.agent.llm import LLMClient
from corridor.schemas.money import fraction_string
from corridor.ui.live_config import deployment, read_credential
from corridor.web import service
from corridor.web.public import DEPLOYMENTS
from corridor.web.state import forget_pair, new_web_session, unwrap

MESSAGES = (
    "给妈妈转500元",
    "这个月钱都花哪了",
    "卡丢了",
    "我都订了什么会员",
    "我爱人生日快到了",
    "看看理财产品",
)


def check(
    deployment_id: str, credential_dir: Path, output: Path, *, client_factory=LLMClient
) -> dict:
    if deployment_id not in DEPLOYMENTS:
        raise ValueError("Deployment is outside the public allow list")
    item = deployment(deployment_id)
    credential = read_credential(credential_dir / item.env_file)
    if credential is None:
        raise ValueError("No usable credential for this deployment")
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, message in enumerate(MESSAGES):
        ws = new_web_session("judge", "zh-CN", browser_id="site-check", tab_id=f"case-{index}")
        client = None
        response = {}
        failure = None
        controller = unwrap(ws.controller)
        try:
            service.login(ws)
            client = client_factory(
                mode="record",
                env_file=None,
                api_key=credential.api_key,
                model=item.model,
                api=item.api,
                api_base=item.api_base,
                provider=item.provider,
                max_output_tokens=item.max_output_tokens,
                max_calls=20,
                max_usd=Fraction(5),
                assumed_price_in=item.price_in,
                assumed_price_out=item.price_out,
                replay_dir=output.parent / (output.stem + "-replay") / str(index),
                context_guard=controller.gateway.vault.ensure_safe,
                tool_namespace="agentdojo-banking-v1.2.2",
            )
            controller.set_client(client)
            unwrap(ws.session).client = client
            rkey = service.send_message(ws, message, 0, f"site-check-{index}")
            response = ws.session.response(ws.rkeys[rkey])
        except Exception as error:
            # Provider exceptions may contain credentials: expose only the type.
            failure = type(error).__name__
        finally:
            forget_pair(ws)
        rows.append({
            "message": message,
            "card": bool(response.get("card")),
            "answer": bool(response.get("message")),
            "status": response.get("status"),
            "needs_clarification": response.get("needs_clarification", False),
            "verdict": {"tier": response.get("tier"), "rule_ids": response.get("rule_ids", [])},
            "bank_dispatches": controller.gateway.bank.execute_count,
            "calls": len(client.calls) if client is not None else 0,
            "api_calls": client.api_calls if client is not None else 0,
            "cost": fraction_string(client.spent_usd) if client is not None else "0/1",
            "currency": item.currency,
            "error": failure,
        })
    result = {"deployment": deployment_id, "rows": rows, "automatic_approvals": False}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", choices=sorted(DEPLOYMENTS))
    parser.add_argument("credential_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = check(args.deployment, args.credential_dir, args.out)
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}))
        return 1
    for row in result["rows"]:
        print(json.dumps(row, ensure_ascii=False))
    return int(any(row["error"] or not (row["card"] or row["answer"]) for row in result["rows"]))


if __name__ == "__main__":
    raise SystemExit(main())
