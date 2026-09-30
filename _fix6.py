# -*- coding: utf-8 -*-
from pathlib import Path

# 1) main.py: правильный текст промо — китайские нейросети
p = Path("main.py")
src = p.read_text(encoding="utf-8")
old = '''            text = ("⚡️ <b>Бесплатные нейросети — без карты и лимитов</b>\\n\\n"
                    "GPT, Claude, Gemini и другие топ-модели в одном боте:\\n"
                    "👉 <b>@iishkogatewaybot</b>\\n\\n"
                    "<i>Подписка на канал — свежие раздачи и ключи каждый день.</i>")'''
new = '''            text = ("⚡️ <b>Бесплатные китайские нейросети</b>\\n\\n"
                    "DeepSeek, Qwen, Kimi, GLM, Doubao, Kling (видео) — "
                    "топ-модели Китая в одном боте, на русском:\\n"
                    "👉 <b>@iishkogatewaybot</b>")'''
assert old in src, "main promo anchor"
p.write_text(src.replace(old, new, 1), encoding="utf-8")
print("main.py: китайские нейросети")

# 2) formatter.py: PROMO под постами
p = Path("formatter.py")
src = p.read_text(encoding="utf-8")
old = "@iishkogatewaybot — бесплатные нейросети"
new = "@iishkogatewaybot — бесплатные китайские нейросети"
assert old in src, "formatter anchor"
p.write_text(src.replace(old, new, 1), encoding="utf-8")
print("formatter.py: PROMO китайские")

# 3) .env: heartbeat off (промо = закреп + под постами, без часового спама)
p = Path(".env")
src = p.read_text(encoding="utf-8")
if "HEARTBEAT_HOURS" not in src:
    src += "HEARTBEAT_HOURS=0\n"
else:
    import re
    src = re.sub(r"HEARTBEAT_HOURS=.*", "HEARTBEAT_HOURS=0", src)
p.write_text(src, encoding="utf-8")
print(".env: HEARTBEAT_HOURS=0")
print("OK")
