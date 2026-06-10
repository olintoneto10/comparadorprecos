#!/usr/bin/env python3
"""
Rastreador de precos — Bambu Lab, Best Buy, Amazon, Walmart, Target, Costco
Roda via GitHub Actions a cada 2 dias.

Solucoes para bloqueio de IP:
- Bambu Lab: ScraperAPI (proxy residencial) + fallback requests direto.
- Best Buy: API oficial gratuita (developer.bestbuy.com) via BESTBUY_API_KEY.
- Walmart/Target/Costco/Best Buy HTML: ScraperAPI proxy residencial via SCRAPER_API_KEY.
- Amazon: cloudscraper + ScraperAPI como fallback.

Como configurar (GitHub > Settings > Secrets and variables > Actions):
  BESTBUY_API_KEY  — chave gratuita de developer.bestbuy.com
  SCRAPER_API_KEY  — chave gratuita de scraperapi.com (1000 req/mes)
"""
import json
import os
import re
import time
import random
import urllib.parse
import requests
from datetime import datetime, timezone

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import cloudscraper
    HAS_CS = True
except ImportError:
    HAS_CS = False
    print("  [aviso] cloudscraper nao instalado")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE  = os.path.join(SCRIPT_DIR, "precos.json")
LISTA_FILE = os.path.join(SCRIPT_DIR, "lista.json")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "")

ORLANDO_ZIP  = "32819"
ORLANDO_STATE = "FL"

def make_scraper():
    if HAS_CS:
        return cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    return requests.Session()

def hdrs(referer=None):
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none" if not referer else "same-origin",
        "Cache-Control": "no-cache",
    }
    if referer:
        h["Referer"] = referer
    return h

def scraperapi_get(url, timeout=70):
    """GET via ScraperAPI (proxy residencial US). Contorna bloqueio de IP.
    Retorna requests.Response com status 200 ou None."""
    if not SCRAPER_API_KEY:
        return None
    try:
        r = requests.get(
            "http://api.scraperapi.com",
            params={"api_key": SCRAPER_API_KEY, "url": url,
                    "keep_headers": "true", "country_code": "us"},
            timeout=timeout,
        )
        print(f"      [ScraperAPI] HTTP {r.status_code}, {len(r.text)} bytes")
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"      [ScraperAPI] {e}")
    return None

def fetch_brl_usd():
    """Busca taxa de cambio USD->BRL. Tenta direto, depois via ScraperAPI."""
    url = "https://economia.awesomeapi.com.br/json/last/USD-BRL"

    def _extrair(r):
        if r and r.status_code == 200:
            try:
                rate = float(r.json().get("USDBRL", {}).get("bid", 0))
                if rate > 1:
                    return rate
            except Exception:
                pass
        return None

    try:
        r = requests.get(url, timeout=10)
        rate = _extrair(r)
        if rate:
            print(f"  [Cambio] USD/BRL = {rate:.4f}")
            return rate
    except Exception as e:
        print(f"  [Cambio] erro direto: {e}")

    r2 = scraperapi_get(url, timeout=30)
    rate = _extrair(r2)
    if rate:
        print(f"  [Cambio] USD/BRL = {rate:.4f} (via ScraperAPI)")
        return rate

    print("  [Cambio] usando fallback 5.80")
    return 5.80

ML_API = "https://api.mercadolibre.com/sites/MLB/search"

def _parse_ml_json(data, query):
    """Extrai preco mediano dos resultados da API do Mercado Livre."""
    results = data.get("results", [])
    itens_novos = [i for i in results if i.get("condition") == "new" and i.get("price", 0) > 10]
    if not itens_novos:
        print(f"      [ML] sem itens novos para '{query[:40]}'")
        return None, None
    precos = sorted(i["price"] for i in itens_novos)
    mediana = precos[len(precos) // 2]
    melhor_url = (itens_novos[0].get("permalink") or
                  "https://www.mercadolivre.com.br/busca?q=" + requests.utils.quote(query))
    print(f"      [ML] {len(precos)} itens: min R${min(precos):.2f}, mediana R${mediana:.2f}")
    return mediana, melhor_url

def _parse_ml_html(html, query):
    """Extrai precos da pagina HTML de busca do Mercado Livre (fallback)."""
    if not HAS_BS4:
        return None, None
    for pattern in [
        r'window\.__PRELOADED_STATE__\s*=\s*(\{.+?\});\s*</script>',
        r'"initialState"\s*:\s*(\{.+?\})\s*[,;]',
        r'window\.ML_PRELOADED_STATE\s*=\s*(\{.+?\})',
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                state = json.loads(m.group(1))
                results = (state.get("results") or
                           state.get("search", {}).get("results") or [])
                precos = [float(r["price"]) for r in results
                          if r.get("price") and float(r["price"]) > 10]
                if precos:
                    precos.sort()
                    return precos[len(precos) // 2], None
            except Exception:
                pass
    soup = BeautifulSoup(html, "lxml")
    precos = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "")
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if not isinstance(item, dict):
                    continue
                offers = item.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price") or offers.get("lowPrice")
                if price:
                    try:
                        v = float(str(price).replace(",", ""))
                        if 10 < v < 100000:
                            precos.append(v)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass
    if precos:
        precos.sort()
        return precos[len(precos) // 2], None
    return None, None

def fetch_mercadolivre(query, max_results=10):
    """Busca preco mediano no Mercado Livre."""
    params = {"q": query, "limit": max_results, "condition": "new"}
    api_url = ML_API + "?" + urllib.parse.urlencode(params)
    ml_hdrs = {"Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9",
               "User-Agent": random.choice(USER_AGENTS)}

    try:
        r = requests.get(ML_API, params=params, headers=ml_hdrs, timeout=15)
        print(f"      [ML] '{query[:40]}': HTTP {r.status_code}")
        if r.status_code == 200:
            return _parse_ml_json(r.json(), query)
    except Exception as e:
        print(f"      [ML] erro direto: {e}")

    r2 = scraperapi_get(api_url)
    if r2:
        try:
            if r2.text.lstrip().startswith("{"):
                return _parse_ml_json(r2.json(), query)
        except Exception as e:
            print(f"      [ML] ScraperAPI parse erro: {e}")

    html_url = "https://www.mercadolivre.com.br/busca?q=" + requests.utils.quote(query)
    r3 = scraperapi_get(html_url)
    if r3 and HAS_BS4:
        p, url = _parse_ml_html(r3.text, query)
        if p:
            print(f"      [ML] R${p:.2f} via HTML scrape")
            return p, html_url

    print(f"      [ML] '{query[:40]}': sem preco")
    return None, None

STORE_INFO = {
    "bambulab":      {"nome": "Bambu Lab US",    "emoji": "\U0001f7e2"},
    "bestbuy":       {"nome": "Best Buy",        "emoji": "\U0001f535"},
    "amazon":        {"nome": "Amazon",          "emoji": "\U0001f7e1"},
    "walmart":       {"nome": "Walmart",         "emoji": "\U0001f536"},
    "target":        {"nome": "Target",          "emoji": "\U0001f3af"},
    "costco":        {"nome": "Costco",          "emoji": "\U0001f534"},
    "newegg":        {"nome": "Newegg",          "emoji": "\U0001f7e0"},
    "bambulab_br":   {"nome": "Bambu Lab BR",    "emoji": "\U0001f1e7\U0001f1f7"},
    "mercadolivre":  {"nome": "Mercado Livre",   "emoji": "\U0001f7e1"},
    "kabum":         {"nome": "Kabum",           "emoji": "\U0001f1e7\U0001f1f7"},
}

def store_info(loja):
    return STORE_INFO.get(loja, {"nome": loja.capitalize(), "emoji": "\U0001f517"})

_BL_BR = "https://br.store.bambulab.com/products/{}"

PRODUCTS = [
    {"id":"p2s-combo", "nome":"Bambu Lab P2S Combo (AMS 2 Pro)", "categoria":"impressora", "qty":1,
     "lojas":{
       "bambulab": {"handle":"p2s", "variant_hint":"combo"},
       "bestbuy":  {"sku":"6647058", "url":"https://www.bestbuy.com/site/bambu-lab-p2s-combo-fdm-3d-printer-with-ams-2-pro/6647058.p"},
       "walmart":  {"query":"Bambu Lab P2S Combo 3D Printer AMS"},
       "target":   {"query":"Bambu Lab P2S 3D Printer"},
       "costco":   {"query":"Bambu Lab P2S 3D Printer"},
     },
     "brasil":{"handle":"p2s","variant_hint":"combo","url_br":_BL_BR.format("p2s"),
               "ml_query":"Bambu Lab P2S Combo AMS impressora 3D"}},
    {"id":"hotend-02-ss", "nome":"Hotend 0.2mm Stainless Steel (P2S)", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"bambu-hotend-h2-p2s", "variant_hint":"0.2"},
     },
     "brasil":{"handle":"bambu-hotend-h2-p2s","variant_hint":"0.2","url_br":_BL_BR.format("bambu-hotend-h2-p2s"),
               "ml_query":"Bambu Lab hotend 0.2mm P2S"}},
    {"id":"hotend-04-hs", "nome":"Hotend 0.4mm Hardened Steel (P2S)", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"bambu-hotend-h2-p2s", "variant_hint":"hardened"},
     },
     "brasil":{"handle":"bambu-hotend-h2-p2s","variant_hint":"hardened","url_br":_BL_BR.format("bambu-hotend-h2-p2s"),
               "ml_query":"Bambu Lab hotend 0.4mm hardened steel P2S"}},
    {"id":"pei-plate", "nome":"Bambu Dual-Texture PEI Plate", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"bambu-dual-texture-pei-plate"},
       "walmart":  {"query":"Bambu Lab PEI Plate Dual Texture"},
     },
     "brasil":{"handle":"bambu-dual-texture-pei-plate","url_br":_BL_BR.format("bambu-dual-texture-pei-plate"),
               "ml_query":"Bambu Lab placa PEI dupla textura"}},
    {"id":"liquid-glue", "nome":"Bambu Liquid Glue", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"liquid-glue-for-build-plate"},
       "amazon":   {"asin":"B0DK6TBF1D"},
       "walmart":  {"query":"Bambu Lab Liquid Glue 3D printer"},
     },
     "brasil":{"handle":"liquid-glue-for-build-plate","url_br":_BL_BR.format("liquid-glue-for-build-plate"),
               "ml_query":"Bambu Lab liquid glue cola placa impressora"}},
    {"id":"nozzle-wiper", "nome":"Nozzle Wiper", "categoria":"acessorio", "qty":2,
     "lojas":{
       "bambulab": {"handle":"nozzle-wiper"},
       "amazon":   {"asin":"B0GSSB8GDQ"},
     },
     "brasil":{"handle":"nozzle-wiper","url_br":_BL_BR.format("nozzle-wiper"),
               "ml_query":"Bambu Lab nozzle wiper limpador bico impressora"}},
    {"id":"pla-silk-red-gold", "nome":"PLA Silk Dual Color (Red-Gold)", "categoria":"filamento", "qty":2,
     "lojas":{
       "bambulab": {"handle":"pla-silk-dual-color", "variant_hint":"red"},
       "amazon":   {"asin":"B0FQPPLP3S"},
       "walmart":  {"query":"Bambu Lab PLA Silk Dual Color Red Gold filament"},
     },
     "brasil":{"handle":"pla-silk-dual-color","variant_hint":"red","url_br":_BL_BR.format("pla-silk-dual-color"),
               "ml_query":"Bambu Lab PLA Silk Dual Color vermelho dourado filamento"}},
    {"id":"pla-silk-blue-purple", "nome":"PLA Silk Dual Color (Blue-Purple)", "categoria":"filamento", "qty":2,
     "lojas":{
       "bambulab": {"handle":"pla-silk-dual-color", "variant_hint":"blue"},
       "amazon":   {"asin":"B0FQPPLP3S"},
       "walmart":  {"query":"Bambu Lab PLA Silk Dual Color Blue Purple filament"},
     },
     "brasil":{"handle":"pla-silk-dual-color","variant_hint":"blue","url_br":_BL_BR.format("pla-silk-dual-color"),
               "ml_query":"Bambu Lab PLA Silk Dual Color azul roxo filamento"}},
    {"id":"pla-matte-charcoal", "nome":"PLA Matte Charcoal", "categoria":"filamento", "qty":2,
     "lojas":{
       "bambulab": {"handle":"pla-matte", "variant_hint":"charcoal"},
       "amazon":   {"asin":"B0G4ZVJDM7"},
       "walmart":  {"query":"Bambu Lab PLA Matte Charcoal filament 1kg"},
     },
     "brasil":{"handle":"pla-matte","variant_hint":"charcoal","url_br":_BL_BR.format("pla-matte"),
               "ml_query":"Bambu Lab PLA Matte filamento 1kg"}},
    {"id":"pla-matte-terracotta", "nome":"PLA Matte Terracotta", "categoria":"filamento", "qty":2,
     "lojas":{
       "bambulab": {"handle":"pla-matte", "variant_hint":"terracotta"},
       "amazon":   {"asin":"B0G5175G82"},
       "walmart":  {"query":"Bambu Lab PLA Matte Terracotta filament 1kg"},
     },
     "brasil":{"handle":"pla-matte","variant_hint":"terracotta","url_br":_BL_BR.format("pla-matte"),
               "ml_query":"Bambu Lab PLA Matte Terracotta filamento 1kg"}},
    {"id":"pla-glow", "nome":"PLA Glow-in-the-Dark", "categoria":"filamento", "qty":1,
     "lojas":{
       "bambulab": {"handle":"pla-glow"},
       "walmart":  {"query":"Bambu Lab PLA Glow in the Dark filament"},
     },
     "brasil":{"handle":"pla-glow","url_br":_BL_BR.format("pla-glow"),
               "ml_query":"Bambu Lab PLA fosforescente glow filamento"}},
    {"id":"pla-basic-black", "nome":"PLA Basic Preto", "categoria":"filamento", "qty":1,
     "lojas":{
       "bambulab": {"handle":"pla-basic-filament", "variant_hint":"black"},
       "amazon":   {"asin":"B0C4GBJCSV"},
       "walmart":  {"query":"Bambu Lab PLA Basic Black filament 1kg"},
     },
     "brasil":{"handle":"pla-basic-filament","variant_hint":"black","url_br":_BL_BR.format("pla-basic-filament"),
               "ml_query":"Bambu Lab PLA Basic preto filamento 1kg"}},
    {"id":"pla-basic-white", "nome":"PLA Basic Branco", "categoria":"filamento", "qty":1,
     "lojas":{
       "bambulab": {"handle":"pla-basic-filament", "variant_hint":"white"},
       "amazon":   {"asin":"B0C4GB1TB1"},
       "walmart":  {"query":"Bambu Lab PLA Basic White filament 1kg"},
     },
     "brasil":{"handle":"pla-basic-filament","variant_hint":"white","url_br":_BL_BR.format("pla-basic-filament"),
               "ml_query":"Bambu Lab PLA Basic branco filamento 1kg"}},
    {"id":"ninja-crispi-pro", "nome":"Ninja Crispi Pro 6-in-1 Glass Air Fryer AS101DG Ash Grey", "categoria":"eletronico", "qty":1,
     "lojas":{
       "amazon":  {"asin":"B0FPPJBKLS"},
       "bestbuy": {"sku":"6604834", "url":"https://www.bestbuy.com/site/ninja-crispi-pro-6-in-1-glass-air-fryer-system/6604834.p"},
       "walmart": {"query":"Ninja Crispi Pro AS101DG Glass Air Fryer Ash Grey"},
       "target":  {"query":"Ninja Crispi Pro AS101DG Air Fryer"},
       "costco":  {"query":"Ninja Crispi Pro Glass Air Fryer"},
     },
     "brasil":{"ml_query":"Ninja Crispi Pro fritadeira vidro"}},
]

STORE_COUPON_SOURCES = {
    "bambulab": [
        ("RetailMeNot",  "https://www.retailmenot.com/view/bambulab.com"),
        ("CouponFollow", "https://couponfollow.com/site/bambulab.com"),
    ],
    "bestbuy": [
        ("RetailMeNot",  "https://www.retailmenot.com/view/bestbuy.com"),
        ("CouponFollow", "https://couponfollow.com/site/bestbuy.com"),
    ],
    "amazon": [
        ("RetailMeNot",  "https://www.retailmenot.com/view/amazon.com"),
    ],
    "walmart": [
        ("RetailMeNot",  "https://www.retailmenot.com/view/walmart.com"),
        ("CouponFollow", "https://couponfollow.com/site/walmart.com"),
    ],
    "target": [
        ("RetailMeNot",  "https://www.retailmenot.com/view/target.com"),
        ("CouponFollow", "https://couponfollow.com/site/target.com"),
    ],
    "costco": [
        ("RetailMeNot",  "https://www.retailmenot.com/view/costco.com"),
        ("CouponFollow", "https://couponfollow.com/site/costco.com"),
    ],
}

_BLACKLIST = {
    "CODE","COUPON","PROMO","DISCOUNT","DEAL","SALE","SAVE","COPY","CLICK","SHOP",
    "CHECK","VIEW","MORE","FREE","SHIP","SHIPPING","OFFER","TODAY","ONLINE","STORE",
    "BEST","GET","USE","APPLY","ENTER","SHOW","VERIFIED","REVEAL","OFF","SOLD",
    "OUT","NEW","HOT","TOP","ALL","THE","DEALS","COUPONS","PROMOS","CODES",
}

def _ok_codigo(code):
    code = re.sub(r"\s+","",code).upper()
    return (code and 4 <= len(code) <= 20
            and re.match(r"^[A-Z0-9][A-Z0-9_\-]*[A-Z0-9]$", code)
            and code not in _BLACKLIST
            and not code.isdigit()
            and not re.match(r"^[A-Z]{1,4}$", code))

_CLASSE_CODIGO = re.compile(
    r"coupon.?code|promo.?code|discount.?code|code.?text|code.?value|"
    r"offer.?code|voucher.?code|promocode|promoCode|CouponCode", re.I)

def extrair_cupons_html(html, fonte_nome):
    if not HAS_BS4:
        return []
    soup = BeautifulSoup(html, "html.parser")
    cupons, vistos = [], set()
    def add(code, desc=""):
        code = re.sub(r"\s+","",code).upper()
        if _ok_codigo(code) and code not in vistos:
            vistos.add(code)
            cupons.append({"codigo":code,"descricao":desc[:80],"fonte":fonte_nome})
    for el in soup.select("[data-code],[data-coupon-code],[data-promo-code]"):
        code=(el.get("data-code") or el.get("data-coupon-code") or el.get("data-promo-code") or "")
        add(code, el.parent.get_text(" ",strip=True)[:80] if el.parent else "")
    for el in soup.find_all(True):
        cls=" ".join(el.get("class",[]))
        if _CLASSE_CODIGO.search(cls):
            add(el.get_text(strip=True))
    for el in soup.find_all(["code","kbd"]):
        txt=el.get_text(strip=True)
        if len(txt)<=20: add(txt)
    for script in soup.find_all("script",type="application/json"):
        text=script.string or ""
        for m in re.finditer(r'"(?:code|promoCode|couponCode|discountCode)"\s*:\s*"([A-Z0-9_\-]{4,20})"',text,re.I):
            add(m.group(1))
    return cupons[:8]

def buscar_cupons(loja):
    sources = STORE_COUPON_SOURCES.get(loja, [])
    todos, vistos = [], set()
    sc = make_scraper()
    for fonte_nome, url in sources:
        try:
            r = sc.get(url, headers=hdrs(), timeout=20)
            if r.status_code == 200:
                encontrados = extrair_cupons_html(r.text, fonte_nome)
                for c in encontrados:
                    if c["codigo"] not in vistos:
                        vistos.add(c["codigo"])
                        c["verificado_em"] = datetime.now(timezone.utc).isoformat()
                        todos.append(c)
                print(f"      {fonte_nome}: {len(encontrados)} cupons")
            else:
                print(f"      {fonte_nome}: HTTP {r.status_code}")
        except Exception as e:
            print(f"      {fonte_nome}: {e}")
        time.sleep(0.8)
    return todos[:10]

def _preco_de_ld(html):
    if not HAS_BS4:
        return None
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "")
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "@graph" in item:
                    items = item["@graph"]
                    continue
                offers = item.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price") or offers.get("lowPrice")
                if price:
                    try:
                        v = float(str(price).replace(",",""))
                        if 0.5 < v < 50000:
                            return v
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass
    return None

def _parse_price_html(html, seletores):
    if not HAS_BS4:
        return None
    soup = BeautifulSoup(html, "lxml")
    for sel in seletores:
        el = soup.select_one(sel)
        if el:
            txt = el.get("content") or el.get_text()
            m = re.search(r"[\d,]+\.\d{2}", txt.replace("$",""))
            if m:
                try:
                    v = float(m.group().replace(",",""))
                    if 0.5 < v < 50000:
                        return v
                except ValueError:
                    pass
    return _preco_de_ld(html)

def _parse_bl_json(r, handle, variant_hint):
    """Extrai preco do JSON da API Shopify do Bambu Lab."""
    if r is None or r.status_code != 200:
        return None, None
    try:
        product = r.json().get("product", {})
    except ValueError:
        print(f"      [BL] {handle}: resposta invalida (nao JSON)")
        return None, None

    title = product.get("title", "")
    variants = product.get("variants", [])
    print(f"      [BL] {handle}: '{title}', {len(variants)} variante(s)")

    if not variants:
        return None, None

    if variant_hint:
        hint = variant_hint.lower()
        for v in variants:
            fields = " ".join([
                v.get("title") or "",
                v.get("option1") or "",
                v.get("option2") or "",
                v.get("option3") or "",
            ]).lower()
            if hint in fields:
                print(f"      [BL] variante match: '{v.get('title')}' = ${v['price']}")
                return float(v["price"]), v["id"]
        nomes = [v.get("title") for v in variants[:6]]
        print(f"      [BL] hint '{variant_hint}' nao encontrado. Variantes: {nomes}")
        disponiveis = [v for v in variants if v.get("available", True)]
        if disponiveis:
            v = min(disponiveis, key=lambda x: float(x["price"]))
            return float(v["price"]), v["id"]

    for v in variants:
        if v.get("available", True):
            return float(v["price"]), v["id"]
    return float(variants[0]["price"]), variants[0]["id"]

def fetch_bambulab(handle, variant_hint=None):
    url = f"https://us.store.bambulab.com/products/{handle}.json"

    r = scraperapi_get(url)
    if r:
        price, vid = _parse_bl_json(r, handle, variant_hint)
        if price:
            return price, vid

    json_hdrs = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://us.store.bambulab.com/",
    }
    for attempt in range(3):
        try:
            r2 = requests.get(url, headers=json_hdrs, timeout=30)
            print(f"      [BL] {handle}: HTTP {r2.status_code}, {len(r2.text)} bytes")
            if r2.status_code == 404:
                if len(r2.text) > 10000:
                    print(f"      [BL] {handle}: bloqueio Cloudflare (configure SCRAPER_API_KEY)")
                else:
                    print(f"      [BL] {handle}: produto nao encontrado")
                break
            if r2.status_code != 200:
                time.sleep(2 ** attempt)
                continue
            price, vid = _parse_bl_json(r2, handle, variant_hint)
            if price:
                return price, vid
            break
        except requests.exceptions.RequestException as e:
            print(f"      [BL] tentativa {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)

    try:
        page_url = f"https://us.store.bambulab.com/products/{handle}"
        sc = make_scraper()
        r3 = sc.get(page_url, headers=hdrs("https://us.store.bambulab.com/"), timeout=30)
        print(f"      [BL] HTML fallback {handle}: HTTP {r3.status_code}, {len(r3.text)} bytes")
        if r3.status_code == 200:
            p, vid = _parse_bl_shopify_html(r3.text, handle, variant_hint)
            if p:
                return p, vid
    except Exception as e:
        print(f"      [BL] HTML fallback erro: {e}")

    return None, None


def _parse_bl_shopify_html(html, handle, variant_hint):
    """Extrai preco da pagina HTML Shopify do Bambu Lab."""
    p = _preco_de_ld(html)
    if p and 0.5 < p < 50000:
        print(f"      [BL] {handle}: ${p} via JSON-LD")
        return p, None

    for pattern in [
        r'(?:var|let|const)\s+\w*[Pp]roduct\w*\s*=\s*(\{[^<]{200,}\})\s*;',
        r'"product"\s*:\s*(\{"id"[^<]{100,}\})\s*[,;}\n]',
        r'window\.__productData\s*=\s*(\{[^<]+\})',
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                variants = data.get("variants", [])
                price = _selecionar_variante(variants, variant_hint, handle)
                if price:
                    print(f"      [BL] {handle}: ${price} via Shopify product JSON embutido")
                    return price, None
            except Exception:
                pass

    if HAS_BS4:
        soup = BeautifulSoup(html, "lxml")
        for script in soup.find_all("script"):
            txt = script.string or ""
            if '"variants"' not in txt and 'variants' not in txt:
                continue
            m = re.search(r'"variants"\s*:\s*(\[[^\]]{50,}\])', txt)
            if m:
                try:
                    variants = json.loads(m.group(1))
                    price = _selecionar_variante(variants, variant_hint, handle)
                    if price:
                        print(f"      [BL] {handle}: ${price} via variantes no script")
                        return price, None
                except Exception:
                    pass

        for sel in [
            "[data-product-price]",
            ".product__price [class*='price']",
            ".price__regular",
            ".product-single__price",
            ".price-item--regular",
            "[class*='ProductPrice']",
            "[class*='product-price']",
            "[itemprop='price']",
        ]:
            el = soup.select_one(sel)
            if el:
                txt = el.get("content") or el.get("data-product-price") or el.get_text()
                m2 = re.search(r"\$?([\d,]+\.?\d{0,2})", txt.strip())
                if m2:
                    try:
                        v = float(m2.group(1).replace(",",""))
                        if 0.5 < v < 50000:
                            print(f"      [BL] {handle}: ${v} via CSS '{sel}'")
                            return v, None
                    except ValueError:
                        pass

    matches = re.findall(r'"price"\s*:\s*(\d+)', html)
    for pc_str in matches:
        pc = int(pc_str)
        if 500 <= pc <= 1000000:
            price = pc / 100
            print(f"      [BL] {handle}: ${price} via regex centavos ({pc_str})")
            return price, None

    for m in re.finditer(r'"price"\s*:\s*"([\d.]+)"', html):
        try:
            v = float(m.group(1))
            if 0.5 < v < 50000:
                print(f"      [BL] {handle}: ${v} via regex dolares")
                return v, None
        except ValueError:
            pass

    print(f"      [BL] {handle}: HTML sem preco reconhecivel")
    return None, None


def _selecionar_variante(variants, variant_hint, handle):
    """Seleciona preco da variante certa."""
    if not variants:
        return None
    primeiro_preco = variants[0].get("price", 0) if isinstance(variants[0], dict) else 0
    divisor = 100 if isinstance(primeiro_preco, int) and primeiro_preco > 1000 else 1

    candidatos = []
    if variant_hint:
        hint = variant_hint.lower()
        for v in variants:
            if not isinstance(v, dict):
                continue
            fields = " ".join([
                str(v.get("title","")).lower(),
                str(v.get("option1","")).lower(),
                str(v.get("option2","")).lower(),
                str(v.get("option3","")).lower(),
                str(v.get("name","")).lower(),
            ])
            if hint in fields:
                candidatos.append(v)
        if candidatos:
            v = min(candidatos, key=lambda x: float(x.get("price",0)))
            p = float(v.get("price", 0)) / divisor
            if 0.5 < p < 50000:
                return round(p, 2)
        nomes = [v.get("title") or v.get("name") for v in variants[:4]]
        print(f"      [BL] hint '{variant_hint}' nao casou. Opcoes: {nomes}")

    disponiveis = [v for v in variants if isinstance(v, dict) and v.get("available", True)]
    candidatos = disponiveis or variants
    v = min(candidatos, key=lambda x: float(x.get("price", 0)) if isinstance(x, dict) else 0)
    if isinstance(v, dict):
        p = float(v.get("price", 0)) / divisor
        if 0.5 < p < 50000:
            return round(p, 2)
    return None

def fetch_kabum(query):
    """Busca preco no Kabum.com.br via ScraperAPI."""
    search_url = "https://www.kabum.com.br/busca?string=" + requests.utils.quote(query)
    r = scraperapi_get(search_url)
    if not r or not HAS_BS4:
        return None, None
    try:
        soup = BeautifulSoup(r.text, "lxml")
        p = _preco_de_ld(r.text)
        if p and p > 10:
            print(f"      [KB] '{query[:35]}': R${p:.2f} via JSON-LD")
            return p, search_url
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd:
            try:
                data = json.loads(nd.string)
                products = (data.get("props", {}).get("pageProps", {})
                            .get("productList", {}).get("data", []))
                precos = [float(p["preco"]) for p in products if p.get("preco") and float(p["preco"]) > 10]
                if precos:
                    precos.sort()
                    preco = precos[len(precos) // 2]
                    print(f"      [KB] '{query[:35]}': R${preco:.2f} ({len(precos)} itens)")
                    return preco, search_url
            except Exception:
                pass
        for sel in [
            "[class*='priceCard']",
            "[class*='Price']",
            "[data-testid='price']",
            ".sc-cdc9b13f-3",
            "[itemprop='price']",
        ]:
            el = soup.select_one(sel)
            if el:
                txt = (el.get("content") or el.get_text()).replace("R$","").replace(".","").replace(",",".")
                m = re.search(r"([\d]+\.?\d{0,2})", txt.strip())
                if m:
                    try:
                        v = float(m.group(1))
                        if v > 10:
                            print(f"      [KB] '{query[:35]}': R${v:.2f} via CSS")
                            return v, search_url
                    except ValueError:
                        pass
    except Exception as e:
        print(f"      [KB] erro: {e}")
    return None, None

def fetch_bestbuy(sku=None, url_produto=None, search_query=None):
    if BESTBUY_API_KEY:
        try:
            if sku:
                api_url = (f"https://api.bestbuy.com/v1/products/{sku}.json"
                           f"?apiKey={BESTBUY_API_KEY}&show=salePrice,regularPrice,onSale,name"
                           f"&postalCode={ORLANDO_ZIP}")
            elif search_query:
                q = requests.utils.quote(search_query)
                api_url = (f"https://api.bestbuy.com/v1/products((search={q}))"
                           f"?apiKey={BESTBUY_API_KEY}&show=salePrice,regularPrice,name"
                           f"&pageSize=5&format=json&postalCode={ORLANDO_ZIP}")
            else:
                api_url = None
            if api_url:
                r = requests.get(api_url, timeout=30)
                print(f"      [BB] API oficial: HTTP {r.status_code}")
                if r.status_code == 200:
                    d = r.json()
                    price = d.get("salePrice") or d.get("regularPrice")
                    if not price and "products" in d:
                        prods = d["products"]
                        if prods:
                            price = prods[0].get("salePrice") or prods[0].get("regularPrice")
                    if price:
                        print(f"      [BB] preco via API oficial: ${price}")
                        return float(price)
        except Exception as e:
            print(f"      [BB] API oficial erro: {e}")

    target_url = url_produto or (f"https://www.bestbuy.com/site/product/{sku}.p" if sku else None)
    if target_url:
        r2 = scraperapi_get(target_url)
        if r2 and HAS_BS4:
            p = _parse_price_html(r2.text, [
                ".priceView-hero-price span[aria-hidden]",
                "[data-testid='customer-price'] span",
                ".priceView-customer-price span",
                "[class*='priceView'] span[aria-hidden]",
            ])
            if p:
                print(f"      [BB] preco via ScraperAPI: ${p}")
                return p

    if sku:
        api = (f"https://www.bestbuy.com/api/tcfb/model.json"
               f"?paths=%5B%5B%22shop%22%2C%22button%22%2C%22skus%22%2C{sku}%2C%22prices%22%5D%5D&method=get")
        try:
            r = requests.get(api, headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json",
                "Referer": "https://www.bestbuy.com/",
            }, timeout=40)
            print(f"      [BB] API interna {sku}: HTTP {r.status_code}")
            if r.status_code == 200:
                prices = (r.json().get("jsonGraph",{}).get("shop",{}).get("button",{})
                          .get("skus",{}).get(str(sku),{}).get("prices",{}))
                for key in ["currentPrice","salePrice","regularPrice"]:
                    val = prices.get(key,{})
                    if isinstance(val, dict): val = val.get("value")
                    if val:
                        print(f"      [BB] preco via API interna: ${val}")
                        return float(val)
        except requests.exceptions.Timeout:
            print(f"      [BB] timeout — IP bloqueado")
            return None
        except Exception as e:
            print(f"      [BB] API interna erro: {e}")
    return None

_AMZ_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#corePrice_desktop .a-price .a-offscreen",
    "#apex_offerDisplay_desktop .a-price .a-offscreen",
    ".priceToPay .a-offscreen",
    "#price_inside_buybox",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    ".a-price.a-text-price .a-offscreen",
    "#buyNewSection .a-price .a-offscreen",
    "[data-asin] .a-price .a-offscreen",
]

def _parse_amazon_html(html, label):
    if not HAS_BS4:
        return None
    soup = BeautifulSoup(html, "lxml")
    for sel in _AMZ_SELECTORS:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text().replace("$","").replace(",","").strip()
            try:
                v = float(txt)
                if 0.5 < v < 50000:
                    print(f"      [AMZ] {label}: ${v} via '{sel}'")
                    return v
            except ValueError:
                pass
    p = _preco_de_ld(html)
    if p:
        print(f"      [AMZ] {label}: ${p} via JSON-LD")
        return p
    title_el = soup.select_one("title")
    print(f"      [AMZ] {label}: sem preco. Titulo='{(title_el.text[:60] if title_el else 'N/A')}'")
    return None

def fetch_amazon(asin):
    url = f"https://www.amazon.com/dp/{asin}"
    amz_cookies = {
        "i18n-prefs": "USD",
        "lc-main": "en_US",
        "x-amzn-marketplace-country": "US",
        "delivery-zipcode": ORLANDO_ZIP,
    }
    sc = make_scraper()
    try:
        h = hdrs("https://www.amazon.com/")
        r = sc.get(url, headers=h, cookies=amz_cookies, timeout=30)
        print(f"      [AMZ] {asin}: HTTP {r.status_code}, {len(r.text)} bytes")
        price = _parse_amazon_html(r.text, asin)
        if price:
            return price
    except Exception as e:
        print(f"      [AMZ] {asin}: {e}")
    r2 = scraperapi_get(url)
    if r2:
        price = _parse_amazon_html(r2.text, f"{asin} [ScraperAPI]")
        if price:
            return price
    return None

def fetch_amazon_url(url):
    if not HAS_BS4:
        return None, url
    sc = make_scraper()
    try:
        r = sc.get(url, headers=hdrs("https://www.amazon.com/"), timeout=30)
        print(f"      [AMZ-URL]: HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")
        for item in soup.select("[data-component-type='s-search-result']"):
            asin = item.get("data-asin", "")
            price_el = item.select_one(".a-price .a-offscreen")
            if price_el:
                txt = price_el.get_text().replace("$","").replace(",","").strip()
                try:
                    v = float(txt)
                    if 0.5 < v < 50000:
                        prod_url = f"https://www.amazon.com/dp/{asin}" if asin else url
                        print(f"      [AMZ-URL]: ${v} (ASIN {asin})")
                        return v, prod_url
                except ValueError:
                    pass
        p = _preco_de_ld(r.text)
        if p:
            return p, url
    except Exception as e:
        print(f"      [AMZ-URL]: {e}")
    return None, url

def _parse_walmart_html(html, url):
    if not HAS_BS4:
        return None, None
    soup = BeautifulSoup(html, "lxml")
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd:
        try:
            data = json.loads(nd.string)
            items = (data.get("props",{}).get("pageProps",{})
                         .get("initialData",{}).get("searchResult",{})
                         .get("itemStacks",[{}])[0].get("items",[]))
            if items:
                item = items[0]
                price = (item.get("priceInfo",{}).get("currentPrice",{})
                             .get("price") or item.get("priceInfo",{}).get("price"))
                slug = item.get("canonicalUrl","")
                prod_url = ("https://www.walmart.com" + slug) if slug else url
                if price:
                    return float(price), prod_url
                print(f"      [WM] __NEXT_DATA__ sem preco")
        except Exception as e:
            print(f"      [WM] parse erro: {e}")
    else:
        print(f"      [WM] __NEXT_DATA__ nao encontrado — provavel bloqueio de IP")
    p = _preco_de_ld(html)
    if p: return p, url
    return None, None

def fetch_walmart(query):
    search_url = "https://www.walmart.com/search?q=" + requests.utils.quote(query)
    r = scraperapi_get(search_url)
    if r:
        print(f"      [WM] '{query[:40]}': ScraperAPI OK")
        price, prod_url = _parse_walmart_html(r.text, search_url)
        if price:
            print(f"      [WM] preco via ScraperAPI: ${price}")
            return price, prod_url
    sc = make_scraper()
    try:
        r2 = sc.get(search_url, headers=hdrs(), timeout=30)
        print(f"      [WM] '{query[:40]}': HTTP {r2.status_code}, {len(r2.text)} bytes")
        return _parse_walmart_html(r2.text, search_url)
    except Exception as e:
        print(f"      [WM] erro: {e}")
    return None, None

def _parse_target_html(html, url):
    if not HAS_BS4:
        return None, None
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        txt = script.string or ""
        m = re.search(r'"currentPrice"\s*:\s*([\d.]+)', txt)
        if m:
            try: return float(m.group(1)), url
            except: pass
    p = _preco_de_ld(html)
    if p: return p, url
    for sel in ["[data-test='product-price']","[class*='styles__CurrentPrice']","[itemprop='price']"]:
        el = soup.select_one(sel)
        if el:
            txt = el.get("content") or el.get_text()
            m = re.search(r"\$?([\d,]+\.?\d{0,2})", txt)
            if m:
                try: return float(m.group(1).replace(",","")), url
                except: pass
    return None, None

def fetch_target(query):
    search_url = "https://www.target.com/s?searchTerm=" + requests.utils.quote(query)
    r = scraperapi_get(search_url)
    if r:
        print(f"      [TG] '{query[:40]}': ScraperAPI OK")
        price, prod_url = _parse_target_html(r.text, search_url)
        if price:
            print(f"      [TG] preco via ScraperAPI: ${price}")
            return price, prod_url
    sc = make_scraper()
    try:
        r2 = sc.get(search_url, headers=hdrs("https://www.target.com/"), timeout=30)
        print(f"      [TG] '{query[:40]}': HTTP {r2.status_code}")
        return _parse_target_html(r2.text, search_url)
    except Exception as e:
        print(f"      [TG] erro: {e}")
    return None, None

def _parse_costco_html(html, url):
    if not HAS_BS4:
        return None, None
    p = _preco_de_ld(html)
    if p: return p, url
    if not HAS_BS4: return None, None
    soup = BeautifulSoup(html, "lxml")
    for sel in [".your-price .value",".e-price","[itemprop='price']",".price"]:
        el = soup.select_one(sel)
        if el:
            txt = el.get("content") or el.get_text()
            m = re.search(r"[\d,]+\.?\d{0,2}", txt.replace("$","").strip())
            if m:
                try:
                    v = float(m.group().replace(",",""))
                    if 0.5 < v < 50000: return v, url
                except: pass
    return None, None

def fetch_costco(query):
    search_url = "https://www.costco.com/CatalogSearch?dept=All&keyword=" + requests.utils.quote(query)
    r = scraperapi_get(search_url)
    if r:
        print(f"      [CC] '{query[:40]}': ScraperAPI OK")
        price, prod_url = _parse_costco_html(r.text, search_url)
        if price:
            print(f"      [CC] preco via ScraperAPI: ${price}")
            return price, prod_url
    sc = make_scraper()
    try:
        r2 = sc.get(search_url, headers=hdrs("https://www.costco.com/"), timeout=30)
        print(f"      [CC] '{query[:40]}': HTTP {r2.status_code}")
        return _parse_costco_html(r2.text, search_url)
    except Exception as e:
        print(f"      [CC] erro: {e}")
    return None, None

def fetch_generica(url):
    if not HAS_BS4 or not url:
        return None
    sc = make_scraper()
    try:
        r = sc.get(url, headers=hdrs(), timeout=25)
        soup = BeautifulSoup(r.text, "lxml")
        p = _preco_de_ld(r.text)
        if p: return p
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("itemprop") or "").lower()
            if "price" in prop:
                val = meta.get("content","")
                m = re.search(r"[\d,]+\.?\d*", val)
                if m:
                    try: return float(m.group().replace(",",""))
                    except: pass
        for sel in ["[itemprop='price']","[data-price]",".price",".product-price"]:
            el = soup.select_one(sel)
            if el:
                txt = el.get("content") or el.get("data-price") or el.get_text()
                m = re.search(r"\$?([\d,]+\.?\d{0,2})", txt.strip())
                if m:
                    try: return float(m.group(1).replace(",",""))
                    except: pass
    except Exception as e:
        print(f"      Generica: {e}")
    return None

def url_para_loja(loja_key, url):
    url = (url or "").strip()
    if not url:
        return None, None
    if "amazon.com" in url or loja_key == "amazon":
        m = re.search(r"/dp/([A-Z0-9]{10})|/gp/product/([A-Z0-9]{10})", url)
        if m:
            return "amazon", {"asin": m.group(1) or m.group(2)}
        return "amazon", {"url": url}
    if "bestbuy.com" in url or loja_key == "bestbuy":
        m = re.search(r"/(\d{6,8})(?:\.p|\?|$|/)", url)
        if m:
            return "bestbuy", {"sku": m.group(1), "url": url}
        return "bestbuy", {"url": url}
    if "walmart.com" in url or loja_key == "walmart":
        return "walmart", {"url": url}
    if "target.com" in url or loja_key == "target":
        return "target", {"url": url}
    if "costco.com" in url or loja_key == "costco":
        return "costco", {"url": url}
    return loja_key, {"url": url}

def load_lista():
    if not os.path.exists(LISTA_FILE):
        return []
    try:
        with open(LISTA_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def processar_item(pid, p, item, now):
    quedas_item = []
    for loja, cfg in p["lojas"].items():
        price        = None
        url_produto  = None
        url_carrinho = None

        if loja == "bambulab":
            price, vid = fetch_bambulab(cfg["handle"], cfg.get("variant_hint"))
            url_produto = f"https://us.store.bambulab.com/products/{cfg['handle']}"
            if vid:
                url_carrinho = f"https://us.store.bambulab.com/cart/{vid}:{p['qty']}"

        elif loja == "bestbuy":
            if "sku" in cfg:
                price = fetch_bestbuy(sku=cfg["sku"], url_produto=cfg.get("url"))
                url_produto = cfg.get("url", f"https://www.bestbuy.com/site/product/{cfg['sku']}.p")
            else:
                url = cfg.get("url", "")
                m = re.search(r"/(\d{6,8})(?:\.p|\?|$)", url)
                sku_found = m.group(1) if m else None
                price = fetch_bestbuy(sku=sku_found, url_produto=url, search_query=cfg.get("query"))
                url_produto = url

        elif loja == "amazon":
            if "asin" in cfg:
                price = fetch_amazon(cfg["asin"])
                url_produto = f"https://www.amazon.com/dp/{cfg['asin']}"
            else:
                price, url_produto = fetch_amazon_url(cfg.get("url",""))

        elif loja == "walmart":
            if "query" in cfg:
                price, found_url = fetch_walmart(cfg["query"])
                url_produto = found_url or "https://www.walmart.com/search?q=" + requests.utils.quote(cfg["query"])
            else:
                price = fetch_generica(cfg.get("url",""))
                url_produto = cfg.get("url")

        elif loja == "target":
            if "query" in cfg:
                price, found_url = fetch_target(cfg["query"])
                url_produto = found_url or "https://www.target.com/s?searchTerm=" + requests.utils.quote(cfg["query"])
            else:
                price = fetch_generica(cfg.get("url",""))
                url_produto = cfg.get("url")

        elif loja == "costco":
            if "query" in cfg:
                price, found_url = fetch_costco(cfg["query"])
                url_produto = found_url or "https://www.costco.com/CatalogSearch?dept=All&keyword=" + requests.utils.quote(cfg["query"])
            else:
                price = fetch_generica(cfg.get("url",""))
                url_produto = cfg.get("url")

        else:
            price = fetch_generica(cfg.get("url",""))
            url_produto = cfg.get("url")

        item["lojas_precos"][loja] = {
            "preco": price,
            "url_produto": url_produto,
            "url_carrinho": url_carrinho,
        }
        si = store_info(loja)
        status = f"${round(price,2)}" if price else "sem preco"
        print(f"    {si['emoji']} {si['nome']}: {status}")
        time.sleep(random.uniform(0.8, 1.5))

    validos = {l: d["preco"] for l, d in item["lojas_precos"].items() if d.get("preco")}
    if validos:
        melhor_loja  = min(validos, key=validos.get)
        melhor_preco = validos[melhor_loja]
        prev = item.get("preco_atual")
        item.update({"preco_atual": melhor_preco, "melhor_loja": melhor_loja})
        hist = item.get("historico", [])
        hist.append({"data": now, "preco": melhor_preco, "loja": melhor_loja})
        item["historico"]    = hist[-90:]
        item["preco_minimo"] = min(h["preco"] for h in item["historico"])
        if prev and melhor_preco < prev:
            pct = (prev - melhor_preco) / prev * 100
            quedas_item.append(f"{p['nome']}: ${prev:.2f} -> ${melhor_preco:.2f} ({pct:.1f}% off)")
    else:
        item["preco_atual"] = None
        item["melhor_loja"] = None

    brasil_cfg = p.get("brasil")
    if brasil_cfg:
        preco_brl, url_brl, loja_nome_brl = None, "", ""
        ml_query = brasil_cfg.get("ml_query")

        if ml_query:
            preco_brl, url_brl = fetch_mercadolivre(ml_query)
            if preco_brl:
                loja_nome_brl = "Mercado Livre"

        if not preco_brl and ml_query:
            preco_brl, url_brl = fetch_kabum(ml_query)
            if preco_brl:
                loja_nome_brl = "Kabum"

        if preco_brl:
            item["brasil"] = {
                "preco_brl":  round(preco_brl, 2),
                "url":        url_brl,
                "loja_nome":  loja_nome_brl,
            }
            print(f"    Brasil ({loja_nome_brl}): R${preco_brl:.2f}")
        else:
            item.setdefault("brasil", None)

    return quedas_item

def main():
    print(f"\n  BESTBUY_API_KEY: {'configurado' if BESTBUY_API_KEY else 'NAO configurado'}")
    print(f"  SCRAPER_API_KEY: {'configurado' if SCRAPER_API_KEY else 'NAO configurado'}")

    data = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)

    now = datetime.now(timezone.utc).isoformat()
    data["atualizado_em"] = now
    data.setdefault("items", {})

    print("\n=== Buscando cambio ===")
    usd_brl = fetch_brl_usd()
    data["cambio"] = {
        "usd_brl":      round(usd_brl, 4),
        "fonte":        "AwesomeAPI",
        "atualizado_em": now,
    }
    quedas = []
    ids_processados = set()

    print("\n=== Rastreando precos ===")
    for p in PRODUCTS:
        pid = p["id"]
        ids_processados.add(pid)
        print(f"\n  {p['nome']}")
        item = data["items"].setdefault(pid, {
            "nome": p["nome"], "categoria": p["categoria"], "qty": p["qty"],
            "lojas_precos": {}, "historico": [],
            "preco_atual": None, "preco_minimo": None, "melhor_loja": None,
        })
        item.update({"nome": p["nome"], "qty": p["qty"]})
        item.setdefault("lojas_precos", {})
        quedas.extend(processar_item(pid, p, item, now))
        data["items"][pid] = item

    lista = load_lista()
    for cfg in lista:
        pid = cfg.get("id")
        if not pid or pid in ids_processados:
            continue
        ids_processados.add(pid)
        nome = cfg.get("nome", "Produto")
        print(f"\n  [Custom] {nome}")
        item = data["items"].setdefault(pid, {
            "nome": nome, "categoria": cfg.get("categoria","geral"),
            "qty": cfg.get("qty",1), "lojas_precos": {}, "historico": [],
            "preco_atual": None, "preco_minimo": None, "melhor_loja": None, "_custom": True,
        })
        item.update({"nome": nome, "qty": cfg.get("qty",1), "_custom": True})
        item.setdefault("lojas_precos", {})
        p_lojas = {}
        for loja_key, loja_cfg in cfg.get("lojas",{}).items():
            url = loja_cfg.get("url","")
            detected, det_cfg = url_para_loja(loja_key, url)
            if detected:
                p_lojas[detected] = det_cfg
        p_mock = {"id":pid,"nome":nome,"categoria":item["categoria"],
                  "qty":item["qty"],"lojas":p_lojas}
        quedas.extend(processar_item(pid, p_mock, item, now))
        data["items"][pid] = item

    total = sum((i.get("preco_atual") or 0) * i.get("qty",1) for i in data["items"].values())
    data["total_estimado"] = round(total, 2)

    iof_spread = 1.04
    for item in data["items"].values():
        br = item.get("brasil")
        if br and br.get("preco_brl"):
            br["preco_usd_equivalente"] = round(br["preco_brl"] / (usd_brl * iof_spread), 2)

    print("\n=== Buscando cupons ===")
    cupons_data = {}
    for loja in ["bambulab","bestbuy","amazon","walmart","target","costco"]:
        si = store_info(loja)
        print(f"  {si['emoji']} {si['nome']}...")
        cupons_data[loja] = buscar_cupons(loja)
    data["cupons"] = cupons_data
    print(f"  Total de cupons: {sum(len(v) for v in cupons_data.values())}")

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\n=== Total estimado: ${total:.2f} ===")
    if quedas:
        print("\n  QUEDAS DETECTADAS:")
        for q in quedas:
            print(f"    {q}")
    print(f"  Salvo em {DATA_FILE}")

if __name__ == "__main__":
    main()
