import json
from pathlib import Path


def write_jsonl(path: Path, records: list[dict], final_newline: bool = True) -> None:
    body = "\n".join(json.dumps(item, separators=(",", ":")) for item in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + ("\n" if final_newline else ""), encoding="utf-8")


def sample_records() -> list[dict]:
    return [
        {"timestamp": "2026-09-08T01:00:00Z", "type": "session_meta", "payload": {"cwd": "C:/work/a"}},
        {"timestamp": "2026-09-08T01:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5"}},
        {
            "timestamp": "2026-09-08T01:00:02Z",
            "type": "token_count",
            "payload": {
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 110,
                    }
                }
            },
        },
        {
            "timestamp": "2026-09-08T01:01:02Z",
            "type": "token_count",
            "payload": {
                "info": {
                    "total_token_usage": {
                        "input_tokens": 170,
                        "cached_input_tokens": 30,
                        "output_tokens": 25,
                        "reasoning_output_tokens": 4,
                        "total_tokens": 195,
                    }
                }
            },
        },
    ]
