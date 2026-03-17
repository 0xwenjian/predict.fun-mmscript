import requests, json, re
url = 'https://predict.fun/market/polymarket-fdv-one-day-after-launch'
resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
html = resp.text
escaped = html.replace('\\"', '"').replace('\\\\', '\\')
for m in re.finditer(r'\"question\":\"([^\"]+)\"', escaped):
    q = m.group(1)
    if '2B' in q or '4B' in q or '6B' in q or '8B' in q or '10B' in q:
        text_before = escaped[:m.start()]
        # Find all IDs before this point
        all_ids = re.findall(r'\"id\":\"(\d+)\"', text_before)
        if all_ids:
            closest_id = all_ids[-1]
            print(f"Nearest ID={closest_id} for Question={q}")
