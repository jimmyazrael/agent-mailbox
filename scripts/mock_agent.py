from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic mock mailbox agent")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--response-index", type=int, required=True)
    parser.add_argument("--responses-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-last-message", type=Path, required=True)
    args = parser.parse_args()

    data = json.loads(args.responses_file.read_text(encoding="utf-8"))
    responses = data.get(args.agent, [])
    if args.response_index < len(responses):
        output = responses[args.response_index]
    else:
        output = f"{args.agent} default mock response\n\nMAILBOX_STATUS: final"
    args.output_last_message.parent.mkdir(parents=True, exist_ok=True)
    args.output_last_message.write_text(output, encoding="utf-8", newline="\n")
    print(json.dumps({"agent": args.agent, "response_index": args.response_index, "prompt_chars": len(args.prompt_file.read_text(encoding="utf-8"))}))


if __name__ == "__main__":
    main()

