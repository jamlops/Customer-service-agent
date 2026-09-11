"""槽位抽取：从用户消息中识别结构化实体。

离线用正则保证确定性；接入真实系统时可替换/叠加 LLM 结构化抽取或 NLU 服务，
只需保持 extract() -> dict 接口不变。
"""
from __future__ import annotations

import re

# 注意：中文两侧不宜用 \b（中文属 \w，会产生错误的词边界），这里用 lookaround。
PATTERNS: dict[str, list[re.Pattern]] = {
    "order_id": [
        re.compile(r"(?<![A-Za-z0-9])(SO\d{6,})(?!\d)", re.I),
        re.compile(r"订单号[是为:：\s]*([A-Za-z]{0,2}\d{6,})", re.I),
    ],
    "phone": [re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")],
    "user_id": [re.compile(r"(?<![A-Za-z0-9])(U\d{3,})(?!\d)", re.I)],
}


class SlotExtractor:
    def extract(self, text: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for key, patterns in PATTERNS.items():
            for pat in patterns:
                m = pat.search(text)
                if m:
                    found[key] = (m.group(1) if m.groups() else m.group(0)).upper()
                    break
        return found
