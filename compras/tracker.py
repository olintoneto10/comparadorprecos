#!/usr/bin/env python3
"""
Rastreador de precos — Bambu Lab, Best Buy, Amazon, Walmart, Target, Costco
Roda via GitHub Actions a cada 2 dias.

Solucoes para bloqueio de IP:
- Bambu Lab: ScraperAPI (proxy residencial) + fallback requests direto.
- Best Buy: API oficial gratuita (developer.bestbuy.com) via BESTBUY_API_KEY.
- Walmart/Target/Costco/Best Buy HTML: ScraperAPI proxy residencial via SCRAPER_API_KEY.
- Amazon/Walmart/Target/Costco: curl_cffi (imita o Chrome) + ScraperAPI como fallback.

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

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False
    print("  [aviso] curl_cffi nao instalado")

# USE_CFFI=0 volta ao cloudscraper antigo
USE_CFFI = os.environ.get("USE_CFFI", "1") == "1"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE  = os.path.join(SCRIPT_DIR, "precos.json")
LISTA_FILE = os.path.join(SCRIPT_DIR, "lista.json")
ALERTAS_CFG    = os.path.join(SCRIPT_DIR, "alertas.json")     # sua configuracao
ALERTA_CORPO   = os.path.join(SCRIPT_DIR, "alerta.md")        # gerado; vira issue no GitHub
ALERTA_TITULO  = os.path.join(SCRIPT_DIR, "alerta_titulo.txt")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

SCRAPER_API_KEY   = os.environ.get("SCRAPER_API_KEY", "")
BESTBUY_API_KEY   = os.environ.get("BESTBUY_API_KEY", "")
ML_APP_ID         = os.environ.get("ML_APP_ID", "")
ML_SECRET_KEY     = os.environ.get("ML_SECRET_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# IDs de afiliado (monetizacao). Vazios = links normais, sem alteracao.
AMAZON_TAG_US = os.environ.get("AMAZON_TAG_US", "")   # ex: meusite-20
AMAZON_TAG_BR = os.environ.get("AMAZON_TAG_BR", "")   # ex: meusite-21
ML_MATT_WORD  = os.environ.get("ML_MATT_WORD", "")    # tag do Mercado Livre Afiliados
ML_MATT_TOOL  = os.environ.get("ML_MATT_TOOL", "")    # id da ferramenta ML Afiliados

ORLANDO_ZIP  = "32819"
ORLANDO_STATE = "FL"

def link_afiliado(url):
    """Anexa parametros de afiliado a URLs de produto. Sem ID configurado, devolve a URL intacta."""
    if not url:
        return url
    try:
        sep = "&" if "?" in url else "?"
        if "amazon.com.br" in url:
            if AMAZON_TAG_BR and "tag=" not in url:
                return f"{url}{sep}tag={AMAZON_TAG_BR}"
        elif "amazon.com" in url:
            if AMAZON_TAG_US and "tag=" not in url:
                return f"{url}{sep}tag={AMAZON_TAG_US}"
        elif "mercadolivre.com.br" in url or "mercadolibre.com" in url:
            if ML_MATT_WORD and "matt_word" not in url:
                extra = f"matt_word={ML_MATT_WORD}"
                if ML_MATT_TOOL:
                    extra += f"&matt_tool={ML_MATT_TOOL}"
                return f"{url}{sep}{extra}"
    except Exception:
        pass
    return url

class _CffiSession:
    """Sessao curl_cffi que imita a impressao digital TLS/HTTP2 de um Chrome real.
    Remove o User-Agent passado pelo chamador: um UA diferente do navegador
    imitado denuncia o bot."""
    def __init__(self):
        self._s = cffi_requests.Session(impersonate="chrome")
        self._fallback = None

    def _plano_b(self, url, headers, **kw):
        """Se o curl_cffi falhar ou for bloqueado, tenta o cloudscraper (que ja funcionava)."""
        if not HAS_CS:
            return None
        if self._fallback is None:
            self._fallback = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False})
        try:
            return self._fallback.get(url, headers=headers, **kw)
        except Exception:
            return None

    def get(self, url, headers=None, **kw):
        h = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
        try:
            r = self._s.get(url, headers=h, **kw)
        except Exception as e:
            print(f"      [cffi] erro, tentando cloudscraper: {str(e)[:80]}")
            r2 = self._plano_b(url, headers, **kw)
            if r2 is not None:
                return r2
            raise
        if r.status_code in (403, 429, 503):
            r2 = self._plano_b(url, headers, **kw)
            if r2 is not None and r2.status_code == 200:
                print(f"      [cffi] HTTP {r.status_code}; cloudscraper conseguiu")
                return r2
        return r

def make_scraper():
    if HAS_CFFI and USE_CFFI:
        return _CffiSession()
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

def scraperapi_get(url, timeout=70, country="us"):
    """GET via ScraperAPI (proxy residencial). Contorna bloqueio de IP.
    Use country='br' para sites brasileiros. Retorna requests.Response ou None."""
    if not SCRAPER_API_KEY:
        return None
    try:
        r = requests.get(
            "http://api.scraperapi.com",
            params={"api_key": SCRAPER_API_KEY, "url": url,
                    "keep_headers": "true", "country_code": country},
            timeout=timeout,
        )
        print(f"      [ScraperAPI] HTTP {r.status_code}, {len(r.text)} bytes")
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"      [ScraperAPI] {e}")
    return None

# ---------------------------------------------------------------------------
# Navegador real (SeleniumBase modo UC) — fallback final contra Cloudflare.
# Usado para Bambu Lab e Best Buy quando ScraperAPI/requests falham.
# Desative com USE_BROWSER=0.
# ---------------------------------------------------------------------------
USE_BROWSER = os.environ.get("USE_BROWSER", "1") == "1"
_BROWSER = None
_BROWSER_FAILED = False
_BROWSER_CACHE = {}

def _get_browser():
    """Inicializa (uma unica vez) o Chrome em modo undetected."""
    global _BROWSER, _BROWSER_FAILED
    if _BROWSER is not None or _BROWSER_FAILED:
        return _BROWSER
    if not USE_BROWSER:
        _BROWSER_FAILED = True
        return None
    try:
        from seleniumbase import Driver
        try:
            _BROWSER = Driver(uc=True, headless=False, xvfb=True)
        except TypeError:
            _BROWSER = Driver(uc=True, headless=True)
        print("      [Browser] Chrome UC iniciado")
    except Exception as e:
        print(f"      [Browser] indisponivel: {e}")
        _BROWSER_FAILED = True
    return _BROWSER

def browser_get(url, label=""):
    """Busca HTML via navegador real (contorna Cloudflare). Retorna page_source ou None."""
    if url in _BROWSER_CACHE:
        return _BROWSER_CACHE[url]
    drv = _get_browser()
    if drv is None:
        return None
    try:
        drv.uc_open_with_reconnect(url, reconnect_time=5)
        time.sleep(2)
        html = drv.page_source or ""
        if "Just a moment" in html[:3000] or "challenge-platform" in html[:5000]:
            try:
                drv.uc_gui_click_captcha()
                time.sleep(4)
                html = drv.page_source or ""
            except Exception:
                pass
        print(f"      [Browser] {label or url[:60]}: {len(html)} bytes")
        _BROWSER_CACHE[url] = html
        return html
    except Exception as e:
        print(f"      [Browser] {label or url[:60]}: {e}")
        return None

def close_browser():
    global _BROWSER
    if _BROWSER is not None:
        try:
            _BROWSER.quit()
            print("  [Browser] encerrado")
        except Exception:
            pass
        _BROWSER = None

AWESOMEAPI_TOKEN = os.environ.get("AWESOMEAPI_TOKEN", "")
_FONTES_REAIS = {"AwesomeAPI", "Banco Central (PTAX)", "open.er-api.com", "Frankfurter (BCE)"}

def _cambio_plausivel(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 3 < v < 10 else None

def _cambio_awesome():
    url = "https://economia.awesomeapi.com.br/json/last/USD-BRL"
    hdr = {"x-api-key": AWESOMEAPI_TOKEN} if AWESOMEAPI_TOKEN else {}
    r = requests.get(url, headers=hdr, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:80]}")
    return r.json().get("USDBRL", {}).get("bid")

def _cambio_ptax():
    """Cotacao oficial do Banco Central (ultimo dia util dos ultimos 7 dias)."""
    from datetime import timedelta
    hoje = datetime.now(timezone.utc).date()
    ini = (hoje - timedelta(days=7)).strftime("%m-%d-%Y")
    fim = hoje.strftime("%m-%d-%Y")
    url = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
           "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
           f"?@dataInicial='{ini}'&@dataFinalCotacao='{fim}'&$format=json")
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    valores = r.json().get("value") or []
    if not valores:
        raise RuntimeError("sem cotacao no periodo")
    return valores[-1].get("cotacaoVenda")

def _cambio_er_api():
    r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json().get("rates", {}).get("BRL")

def _cambio_frankfurter():
    r = requests.get("https://api.frankfurter.app/latest?from=USD&to=BRL", timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json().get("rates", {}).get("BRL")

def fetch_brl_usd(anterior=None):
    """Cotacao USD->BRL tentando varias fontes gratuitas.
    Retorna (taxa, fonte, data_da_cotacao). Se todas falharem, reaproveita a
    ultima cotacao real salva (com a data dela) antes de cair no valor fixo."""
    agora = datetime.now(timezone.utc).isoformat()
    for nome, fn in [("AwesomeAPI", _cambio_awesome),
                     ("Banco Central (PTAX)", _cambio_ptax),
                     ("open.er-api.com", _cambio_er_api),
                     ("Frankfurter (BCE)", _cambio_frankfurter)]:
        try:
            rate = _cambio_plausivel(fn())
            if rate:
                print(f"  [Cambio] USD/BRL = {rate:.4f} ({nome})")
                return rate, nome, agora
            print(f"  [Cambio] {nome}: valor invalido")
        except Exception as e:
            print(f"  [Cambio] {nome} falhou: {str(e)[:100]}")

    if anterior and anterior.get("fonte") in _FONTES_REAIS:
        rate = _cambio_plausivel(anterior.get("usd_brl"))
        if rate:
            print(f"  [Cambio] todas as fontes falharam; mantendo ultima cotacao real "
                  f"{rate:.4f} de {anterior.get('atualizado_em','?')[:10]}")
            return rate, anterior["fonte"], anterior.get("atualizado_em", agora)

    print("  [Cambio] todas as fontes falharam; usando valor fixo 5.80")
    return 5.80, "valor fixo (todas as fontes falharam)", agora

ML_API      = "https://api.mercadolibre.com/sites/MLB/search"
ML_ITEM_API = "https://api.mercadolibre.com/items/{}"

_ML_TOKEN        = None
_ML_TOKEN_EXPIRY = 0.0

def get_ml_token():
    """Obtém (ou reutiliza) token OAuth2 client_credentials do Mercado Livre.
    Bypassa o bloqueio de IP de datacenters que afeta chamadas sem autenticação."""
    global _ML_TOKEN, _ML_TOKEN_EXPIRY
    if not ML_APP_ID or not ML_SECRET_KEY:
        return None
    now = time.time()
    if _ML_TOKEN and now < _ML_TOKEN_EXPIRY - 60:
        return _ML_TOKEN
    try:
        r = requests.post(
            "https://api.mercadolibre.com/oauth/token",
            data={"grant_type": "client_credentials",
                  "client_id": ML_APP_ID,
                  "client_secret": ML_SECRET_KEY},
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if r.status_code == 200:
            d = r.json()
            _ML_TOKEN = d.get("access_token")
            _ML_TOKEN_EXPIRY = now + d.get("expires_in", 21600)
            print(f"      [ML] token obtido (expira em {d.get('expires_in',0)}s)")
            return _ML_TOKEN
        print(f"      [ML] erro ao obter token: HTTP {r.status_code} — {r.text[:120]}")
    except Exception as e:
        print(f"      [ML] erro token: {e}")
    return None

def _ml_auth_headers():
    token = get_ml_token()
    h = {"Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9",
         "User-Agent": random.choice(USER_AGENTS)}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def _ml_item_price(item_id):
    """Busca preco exato de um item ML pelo ID."""
    try:
        r = requests.get(ML_ITEM_API.format(item_id),
                         headers=_ml_auth_headers(), timeout=10)
        if r.status_code == 200:
            d = r.json()
            price = d.get("price") or d.get("sale_price")
            if price and float(price) > 10:
                return float(price)
    except Exception:
        pass
    return None

def _parse_ml_json(data, query):
    """Extrai menor preco dos resultados da API do Mercado Livre."""
    results = data.get("results", [])
    itens_novos = [i for i in results
                   if i.get("condition") == "new"
                   and i.get("price", 0) > 10
                   and i.get("available_quantity", 1) > 0]
    if not itens_novos:
        print(f"      [ML] sem itens novos para '{query[:40]}'")
        return None, None
    itens_novos.sort(key=lambda i: i["price"])
    melhor = itens_novos[0]
    preco = _ml_item_price(melhor["id"]) or melhor["price"]
    url = melhor.get("permalink") or "https://www.mercadolivre.com.br/busca?q=" + requests.utils.quote(query)
    print(f"      [ML] {len(itens_novos)} itens novos: menor R${preco:.2f} (id={melhor['id']})")
    return preco, url

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
    """Busca menor preco novo no Mercado Livre via API oficial."""
    params = {"q": query, "limit": max_results, "condition": "new", "sort": "price_asc"}
    api_url = ML_API + "?" + urllib.parse.urlencode(params)

    # Tentativa 1: API autenticada com token OAuth2 (bypassa bloqueio de IP de datacenter)
    try:
        r = requests.get(ML_API, params=params, headers=_ml_auth_headers(), timeout=15)
        print(f"      [ML] '{query[:40]}': HTTP {r.status_code}" +
              (" (autenticado)" if ML_APP_ID else ""))
        if r.status_code == 200:
            return _parse_ml_json(r.json(), query)
    except Exception as e:
        print(f"      [ML] erro direto: {e}")

    # Tentativa 2: ScraperAPI como proxy (se cota disponivel)
    r2 = scraperapi_get(api_url, country="br")
    if r2:
        try:
            if r2.text.lstrip().startswith("{"):
                return _parse_ml_json(r2.json(), query)
        except Exception as e:
            print(f"      [ML] ScraperAPI parse erro: {e}")

    # Tentativa 3: HTML scrape via ScraperAPI
    html_url = "https://www.mercadolivre.com.br/busca?q=" + requests.utils.quote(query)
    r3 = scraperapi_get(html_url, country="br")
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
       "bambulab": {"handle":"bambu-hotend-h2-p2s", "variant_hint":"p2s 0.2"},
     },
     "brasil":{"handle":"bambu-hotend-h2-p2s","variant_hint":"0.2","url_br":_BL_BR.format("bambu-hotend-h2-p2s"),
               "ml_query":"Bambu Lab hotend 0.2mm P2S"}},
    {"id":"hotend-04-hs", "nome":"Hotend 0.4mm Hardened Steel (P2S)", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"bambu-hotend-h2-p2s", "variant_hint":"p2s 0.4 hardened"},
     },
     "brasil":{"handle":"bambu-hotend-h2-p2s","variant_hint":"hardened","url_br":_BL_BR.format("bambu-hotend-h2-p2s"),
               "ml_query":"Bambu Lab hotend 0.4mm hardened steel P2S"}},
    {"id":"pei-plate", "nome":"Bambu Dual-Texture PEI Plate (P2S)", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"bambu-dual-texture-pei-plate", "variant_hint":"p2s"},
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
    {"id":"nozzle-wiper", "nome":"Nozzle Wiper (P2S)", "categoria":"acessorio", "qty":2, "preco_min":2,
     "lojas":{
       "bambulab": {"handle":"nozzle-wiper", "variant_hint":"p2s"},
       "amazon":   {"asin":"B0GSSB8GDQ"},
     },
     "brasil":{"handle":"nozzle-wiper","url_br":_BL_BR.format("nozzle-wiper"),
               "ml_query":"Bambu Lab nozzle wiper limpador bico impressora"}},
    # --- H2C: impressora e os mesmos acessorios da P2S, na versao H2C ---
    {"id":"h2c-combo", "nome":"Bambu Lab H2C Combo", "categoria":"impressora", "qty":1,
     "lojas":{
       "bambulab": {"handle":"h2c", "variant_hint":"combo"},
       "walmart":  {"query":"Bambu Lab H2C Combo 3D Printer"},
     },
     "brasil":{"handle":"h2c","variant_hint":"combo","url_br":_BL_BR.format("h2c"),
               "ml_query":"Bambu Lab H2C Combo impressora 3D"}},
    {"id":"hotend-02-ss-h2c", "nome":"Hotend 0.2mm Stainless Steel (H2C)", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"bambu-hotend-h2-p2s", "variant_hint":"h2c 0.2"},
     },
     "brasil":{"ml_query":"Bambu Lab hotend 0.2mm H2C"}},
    {"id":"hotend-04-hs-h2c", "nome":"Hotend 0.4mm Hardened Steel (H2C)", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"bambu-hotend-h2-p2s", "variant_hint":"h2c 0.4 hardened"},
     },
     "brasil":{"ml_query":"Bambu Lab hotend 0.4mm hardened steel H2C"}},
    {"id":"pei-plate-h2c", "nome":"Bambu Dual-Texture PEI Plate (H2C)", "categoria":"acessorio", "qty":1,
     "lojas":{
       "bambulab": {"handle":"bambu-dual-texture-pei-plate", "variant_hint":"h2c"},
     },
     "brasil":{"ml_query":"Bambu Lab placa PEI dupla textura H2C"}},
    {"id":"nozzle-wiper-h2c", "nome":"Nozzle Wiper (H2C)", "categoria":"acessorio", "qty":2, "preco_min":2,
     "lojas":{
       "bambulab": {"handle":"nozzle-wiper", "variant_hint":"h2c"},
       "amazon":   {"asin":"B0GSSB8GDQ"},   # anuncio serve H2D/H2C/P2S
     },
     "brasil":{"ml_query":"Bambu Lab nozzle wiper H2C"}},
    {"id":"pla-silk-red-gold", "nome":"PLA Silk Dual Color (Red-Gold)", "categoria":"filamento", "qty":2,
     "lojas":{
       "bambulab": {"handle":"pla-silk-dual-color", "listar": True},   # cor a definir
       "amazon":   {"asin":"B0FQPPLP3S"},
       "walmart":  {"query":"Bambu Lab PLA Silk Dual Color Red Gold filament"},
     },
     "brasil":{"handle":"pla-silk-dual-color","variant_hint":"red","url_br":_BL_BR.format("pla-silk-dual-color"),
               "ml_query":"Bambu Lab PLA Silk Dual Color vermelho dourado filamento"}},
    {"id":"pla-silk-blue-purple", "nome":"PLA Silk Dual Color (Blue-Purple)", "categoria":"filamento", "qty":2,
     "lojas":{
       "bambulab": {"handle":"pla-silk-dual-color", "listar": True},   # cor a definir
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
       "walmart":  {"query":"Bambu Lab PLA Glow in the Dark filament", "exige":["pla","glow"]},
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
       "walmart": {"query":"Ninja Crispi Pro AS101DG Glass Air Fryer Ash Grey", "exige":["crispi","pro","6-in-1"]},
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

# ---------------------------------------------------------------------------
# Extrator de preco via Claude (Anthropic) — fallback inteligente.
# Usado quando o HTML foi baixado com sucesso mas os parsers CSS/regex falham
# (tipico do Bambu Lab/Shopify). NAO contorna bloqueio de IP: so funciona
# quando ja existe HTML valido. Configure ANTHROPIC_API_KEY para ativar.
# ---------------------------------------------------------------------------
_CLAUDE_CACHE = {}

def _num_de_texto(texto):
    """Converte a resposta do Claude num float, tratando separadores BR/US."""
    t = (texto or "").strip().lower()
    if "null" in t or not t:
        return None
    t = re.sub(r"[^\d.,]", "", t)
    if not t:
        return None
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):        # 1.299,90 -> BR
            t = t.replace(".", "").replace(",", ".")
        else:                                   # 1,299.90 -> US
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{1,2}$", t) else t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None

def _reduzir_html_para_claude(html, max_chars=45000):
    """Reduz o HTML preservando trechos com preco (scripts JSON + texto visivel)."""
    if not html:
        return ""
    if not HAS_BS4:
        return html[:max_chars]
    try:
        soup = BeautifulSoup(html, "lxml")
        partes = []
        for script in soup.find_all("script"):
            txt = script.string or ""
            low = txt.lower()
            if txt and ("price" in low or "variants" in low or "offers" in low):
                partes.append(txt[:8000])
        for tag in soup(["script", "style", "noscript", "svg", "path", "head", "footer"]):
            tag.decompose()
        partes.append(soup.get_text(" ", strip=True))
        return ("\n---\n".join(partes))[:max_chars]
    except Exception:
        return html[:max_chars]

def fetch_price_claude(html, produto_nome, moeda="USD", preco_min=1, loja=""):
    """Extrai o preco de venda de um HTML dificil usando Claude. Retorna float ou None."""
    if not ANTHROPIC_API_KEY or not html:
        return None
    blob = _reduzir_html_para_claude(html)
    if len(blob) < 50:
        return None
    cache_key = (loja, produto_nome, hash(blob))
    if cache_key in _CLAUDE_CACHE:
        return _CLAUDE_CACHE[cache_key]
    prompt = (
        f"Voce recebe o conteudo (texto + trechos de script) de uma pagina de produto de e-commerce.\n"
        f'Produto procurado: "{produto_nome}"\n'
        f"Moeda esperada: {moeda}. Preco minimo plausivel: {preco_min}.\n\n"
        f"Extraia o PRECO DE VENDA ATUAL do produto principal (o valor que o cliente pagaria hoje, "
        f"ja com desconto se houver, sem frete). Ignore acessorios, itens relacionados, "
        f"'comprados juntos', parcelas, precos antigos riscados e assinaturas.\n"
        f"Se a pagina for bloqueio/captcha/erro, ou nao houver preco claro do produto, responda NULL.\n\n"
        f"Responda SOMENTE com o numero usando ponto como separador decimal (ex: 799.99) ou NULL. "
        f"Sem simbolo de moeda, sem texto extra.\n\nCONTEUDO:\n{blob}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 20,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45,
        )
        if r.status_code != 200:
            print(f"      [Claude] HTTP {r.status_code}: {r.text[:120]}")
            return None
        texto = "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
        v = _num_de_texto(texto)
        if v is None:
            print(f"      [Claude] {loja} '{produto_nome[:28]}': sem preco (resp='{texto[:20]}')")
            _CLAUDE_CACHE[cache_key] = None
            return None
        if v < preco_min:
            print(f"      [Claude] {loja}: {v} abaixo do minimo {preco_min}, descartado")
            _CLAUDE_CACHE[cache_key] = None
            return None
        print(f"      [Claude] {loja} '{produto_nome[:28]}': {moeda} {v}")
        _CLAUDE_CACHE[cache_key] = v
        return v
    except Exception as e:
        print(f"      [Claude] erro: {e}")
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

_BL_PAGINAS = {}   # handle -> HTML (uma requisicao por pagina, reaproveitada entre variantes)

def _bl_pagina(handle):
    """Baixa a pagina do produto UMA vez por atualizacao. Tenta curl_cffi
    (com uma nova tentativa se o site pedir calma), depois o navegador real."""
    if handle in _BL_PAGINAS:
        return _BL_PAGINAS[handle]
    url = f"https://us.store.bambulab.com/products/{handle}"
    html = None
    for tentativa in range(2):
        try:
            r = make_scraper().get(url, headers=hdrs("https://us.store.bambulab.com/"), timeout=30)
            print(f"      [BL] {handle}: HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code == 200 and len(r.text) > 10000:
                html = r.text
                break
            if r.status_code == 429 and tentativa == 0:
                time.sleep(8)        # muitas requisicoes: espera e tenta uma vez mais
                continue
            break
        except Exception as e:
            print(f"      [BL] {handle}: {str(e)[:80]}")
            break
    if not html:
        html = browser_get(url, f"BL {handle}")
        if html and len(html) < 10000:
            html = None
    _BL_PAGINAS[handle] = html
    time.sleep(1.5)                  # espaca as paginas do Bambu Lab
    return html

def _bl_variantes_ld(html):
    """Variantes (nome, preco, disponivel) do JSON-LD da pagina do Bambu Lab.
    A loja usa ProductGroup com hasVariant; tambem aceita Product com offers."""
    variantes = []
    if not html:
        return variantes
    for bloco in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                            html, re.DOTALL | re.I):
        try:
            dados = json.loads(bloco.strip())
        except Exception:
            continue
        itens = dados if isinstance(dados, list) else [dados]
        for it in list(itens):
            if isinstance(it, dict) and "@graph" in it:
                itens.extend(it["@graph"])
        for it in itens:
            if not isinstance(it, dict):
                continue
            tipo = str(it.get("@type", ""))
            fontes = []
            if "ProductGroup" in tipo:
                for v in it.get("hasVariant") or []:
                    if isinstance(v, dict):
                        fontes.append((v.get("name") or it.get("name") or "", v.get("offers")))
            elif "Product" in tipo:
                ofs = it.get("offers")
                ofs = ofs if isinstance(ofs, list) else [ofs]
                for o in ofs:
                    if isinstance(o, dict):
                        fontes.append((o.get("name") or o.get("sku") or it.get("name") or "", o))
            for nome, o in fontes:
                if isinstance(o, list):
                    o = o[0] if o else {}
                if not isinstance(o, dict):
                    continue
                try:
                    preco = float(str(o.get("price") or o.get("lowPrice") or "").replace(",", ""))
                except ValueError:
                    continue
                if 0.5 < preco < 50000:
                    disp = "OutOfStock" not in str(o.get("availability", ""))
                    variantes.append((str(nome), preco, disp))
    return variantes

def _eh_refil(nome):
    n = nome.lower()
    return "refill" in n or "without spool" in n

def _parte_variavel(nome, variantes):
    """Parte do nome que muda entre as variantes. Ex.: em
    'Bambu Hotend - H2/P2S/X2D - Standard Flow / X2D / 0.2mm' o comeco comum
    (que cita P2S em todas) nao conta; so 'X2D / 0.2mm' e comparado."""
    nomes = {v[0] for v in variantes}
    if len(nomes) < 2:
        return nome.lower()
    comum = os.path.commonprefix(sorted(nomes))
    corte = max(comum.rfind(" - "), comum.rfind(" / "))
    inicio = corte + 3 if corte >= 0 else 0
    return nome[inicio:].lower()

def _bl_escolher_variante(variantes, hint):
    """Variante que bate com o hint (todas as palavras: impressora, bico, cor).
    Refil (sem carretel) fica de fora, para comparar igual com Amazon/Walmart.
    Entre as que sobram: disponivel primeiro, depois a mais barata."""
    cand = [v for v in variantes if not _eh_refil(v[0])]
    if hint:
        partes = hint.lower().split()
        cand = [v for v in cand if all(t in _parte_variavel(v[0], variantes) for t in partes)]
    if not cand:
        return None
    return sorted(cand, key=lambda v: (not v[2], v[1]))[0]

def _bl_listar_variantes(handle, variantes, motivo):
    """Mostra no log todas as variantes (sem refil), para escolher o filtro certo."""
    nomes = sorted({v[0] for v in variantes if not _eh_refil(v[0])})
    print(f"      [BL] {handle}: {motivo}. Variantes disponiveis ({len(nomes)}):")
    for n in nomes:
        preco = min(v[1] for v in variantes if v[0] == n)
        print(f"         - {n}  ${preco}")

_BL_LISTADOS = set()   # cada pagina so e listada uma vez no log

def fetch_bambulab(handle, variant_hint=None, nome=None, listar=False):
    html = _bl_pagina(handle)
    if not html:
        print(f"      [BL] {handle}: pagina indisponivel")
        return None, None

    variantes = _bl_variantes_ld(html)
    if variantes:
        if listar:
            if handle not in _BL_LISTADOS:
                _BL_LISTADOS.add(handle)
                _bl_listar_variantes(handle, variantes, "modo listar (variante a definir)")
            return None, None
        v = _bl_escolher_variante(variantes, variant_hint)
        if v:
            print(f"      [BL] {handle}: '{v[0][:70]}' ${v[1]} ({len(variantes)} variantes)")
            return v[1], None
        # A pagina tem dados estruturados e nenhuma variante e o produto pedido:
        # nao vale pedir ao Claude para adivinhar (ele escolheria uma variante errada).
        if handle not in _BL_LISTADOS:
            _BL_LISTADOS.add(handle)
            _bl_listar_variantes(handle, variantes, f"nenhuma variante com '{variant_hint}'")
        else:
            print(f"      [BL] {handle}: nenhuma variante com '{variant_hint}' (lista acima)")
        return None, None

    # Pagina sem dados estruturados: Claude le a mesma pagina (sem nova requisicao)
    desc = " ".join(filter(None, [nome or handle, variant_hint]))
    p = fetch_price_claude(html, desc, moeda="USD", preco_min=1, loja="BL")
    if p:
        return p, None
    return None, None


def fetch_bambulab_br(handle, variant_hint=None):
    """Busca preco na loja Bambu Lab Brasil (br.store.bambulab.com) em BRL."""
    url = f"https://br.store.bambulab.com/products/{handle}.json"
    ml_hdrs = {"Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9",
               "User-Agent": random.choice(USER_AGENTS),
               "Referer": "https://br.store.bambulab.com/"}
    r = scraperapi_get(url, country="br")
    if r:
        price, _ = _parse_bl_json(r, handle, variant_hint)
        if price:
            print(f"      [BL-BR] {handle}: R${price:.2f} via ScraperAPI")
            return round(price, 2), f"https://br.store.bambulab.com/products/{handle}"
    try:
        r2 = requests.get(url, headers=ml_hdrs, timeout=20)
        print(f"      [BL-BR] {handle}: HTTP {r2.status_code}")
        if r2.status_code == 200:
            price, _ = _parse_bl_json(r2, handle, variant_hint)
            if price:
                print(f"      [BL-BR] {handle}: R${price:.2f}")
                return round(price, 2), f"https://br.store.bambulab.com/products/{handle}"
    except Exception as e:
        print(f"      [BL-BR] {handle}: {e}")
    return None, None

def fetch_amazon_br(asin_br=None, query=None):
    """Busca preco na Amazon.com.br via ScraperAPI."""
    if asin_br:
        url = f"https://www.amazon.com.br/dp/{asin_br}"
    elif query:
        url = "https://www.amazon.com.br/s?k=" + requests.utils.quote(query)
    else:
        return None, None
    r = scraperapi_get(url, country="br")
    if not r or not HAS_BS4:
        return None, None
    try:
        soup = BeautifulSoup(r.text, "lxml")
        if asin_br:
            for sel in ["#priceblock_ourprice", "#priceblock_dealprice",
                        ".a-price .a-offscreen", "[data-asin] .a-price .a-offscreen"]:
                el = soup.select_one(sel)
                if el:
                    txt = el.get_text().replace("R$","").replace(".","").replace(",",".")
                    m = re.search(r"[\d]+\.?\d{0,2}", txt.strip())
                    if m:
                        v = float(m.group())
                        if v > 10:
                            print(f"      [AMZ-BR] {asin_br}: R${v:.2f}")
                            return v, url
        else:
            p = _preco_de_ld(r.text)
            if p and p > 10:
                print(f"      [AMZ-BR] busca '{query[:35]}': R${p:.2f}")
                return p, url
    except Exception as e:
        print(f"      [AMZ-BR] erro: {e}")
    print(f"      [AMZ-BR] sem preco")
    return None, None

def fetch_kabum(query):
    """Busca preco no Kabum.com.br via ScraperAPI."""
    search_url = "https://www.kabum.com.br/busca?string=" + requests.utils.quote(query)
    r = scraperapi_get(search_url, country="br")
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
            r = make_scraper().get(api, headers={
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
        except Exception as e:
            if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                print(f"      [BB] timeout — IP bloqueado")
            else:
                print(f"      [BB] API interna erro: {e}")

    # Fallback final: navegador real (contorna Cloudflare/bloqueio de IP)
    if target_url:
        html = browser_get(target_url, f"BB {sku or target_url[:40]}")
        if html and len(html) > 10000:
            p = _parse_price_html(html, [
                ".priceView-hero-price span[aria-hidden]",
                "[data-testid='customer-price'] span",
                ".priceView-customer-price span",
                "[class*='priceView'] span[aria-hidden]",
                "[data-testid='large-price'] span",
            ])
            if p:
                print(f"      [BB] preco via navegador: ${p}")
                return p
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

def fetch_amazon(asin, nome=None):
    url = f"https://www.amazon.com/dp/{asin}"
    amz_cookies = {
        "i18n-prefs": "USD",
        "lc-main": "en_US",
        "x-amzn-marketplace-country": "US",
        "delivery-zipcode": ORLANDO_ZIP,
    }
    melhor_html = ""
    sc = make_scraper()
    try:
        h = hdrs("https://www.amazon.com/")
        r = sc.get(url, headers=h, cookies=amz_cookies, timeout=30)
        print(f"      [AMZ] {asin}: HTTP {r.status_code}, {len(r.text)} bytes")
        price = _parse_amazon_html(r.text, asin)
        if price:
            return price
        if len(r.text) > len(melhor_html):
            melhor_html = r.text
    except Exception as e:
        print(f"      [AMZ] {asin}: {e}")
    r2 = scraperapi_get(url)
    if r2:
        price = _parse_amazon_html(r2.text, f"{asin} [ScraperAPI]")
        if price:
            return price
        if len(r2.text) > len(melhor_html):
            melhor_html = r2.text
    # Fallback inteligente: Claude le o HTML que baixamos mas nao parseamos
    p = fetch_price_claude(melhor_html, nome or asin, moeda="USD", preco_min=1, loja="AMZ")
    if p:
        return p
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

_STOPWORDS_BUSCA = {"the", "for", "and", "with", "in", "of", "de", "da", "do",
                    "kg", "1kg", "3d"}
# Palavras que aparecem em quase todo anuncio e nao identificam o produto
_GENERICAS_BUSCA = {"filament", "printer", "spool", "lab"}
# Marcas: se a busca tem a marca, o item tem que ser dela
_MARCAS_BUSCA = {"bambu", "ninja"}
# Acessorio de terceiros "para Bambu Lab" / "compativel com"
_RE_TERCEIROS = re.compile(r"\bfor\s+bambu\b|\bcompatible\b|\bfits\s+bambu\b", re.I)
_SINONIMOS = {"gray": "grey", "colour": "color"}

def _tokens(texto):
    return {_SINONIMOS.get(t, t) for t in re.findall(r"[a-z0-9]+", (texto or "").lower())
            if len(t) >= 2 and t not in _STOPWORDS_BUSCA}

def _item_bate_com_busca(nome, query, exige=None):
    """Regra rigida: marca igual, nao e de terceiros e TODAS as palavras
    que definem o produto (tipo, modelo, cor) aparecem no nome."""
    nome_t = _tokens(nome)
    busca_t = _tokens(query)
    marcas = busca_t & _MARCAS_BUSCA
    if marcas and not marcas <= nome_t:
        return False
    if _RE_TERCEIROS.search(nome or ""):
        return False
    chave = set(exige) if exige else (busca_t - _GENERICAS_BUSCA - _MARCAS_BUSCA)
    nome_norm = re.sub(r"[\s\-]+", "-", (nome or "").lower())
    for e in chave:
        if re.fullmatch(r"[a-z0-9]+", e):
            if _SINONIMOS.get(e, e) not in nome_t:
                return False
        elif re.sub(r"[\s\-]+", "-", e.lower()) not in nome_norm:   # expressao, ex: "6-in-1"
            return False
    return True

def _wm_preco_item(item):
    """Preco de um item da busca do Walmart (o formato do JSON varia entre paginas)."""
    pi = item.get("priceInfo") or {}
    cur = pi.get("currentPrice") or {}
    for v in (cur.get("price"), item.get("price"), pi.get("price"),
              cur.get("priceString"), pi.get("linePrice"), pi.get("itemPrice")):
        if v in (None, "", 0):
            continue
        p = v if isinstance(v, (int, float)) else _num_de_texto(str(v))
        if p and 0.5 < float(p) < 50000:
            return float(p)
    return None

def _wm_itens(data):
    """Todos os itens de todas as pilhas de resultados (nao so a primeira)."""
    sr = (data.get("props", {}).get("pageProps", {})
              .get("initialData", {}).get("searchResult", {}))
    for stack in sr.get("itemStacks") or []:
        for it in (stack or {}).get("items") or []:
            if isinstance(it, dict):
                yield it

def _parse_walmart_html(html, url, query="", exige=None):
    """Entre os resultados com preco, fica so com os que batem com o produto
    (regra rigida) e devolve o mais barato. Na duvida, sem preco."""
    if not HAS_BS4:
        return None, None
    soup = BeautifulSoup(html, "lxml")
    nd = soup.find("script", id="__NEXT_DATA__")
    if not nd:
        print(f"      [WM] __NEXT_DATA__ nao encontrado — provavel bloqueio de IP")
        return None, None
    try:
        data = json.loads(nd.string)
    except Exception as e:
        print(f"      [WM] parse erro: {e}")
        return None, None

    com_preco, validos = 0, []
    for it in _wm_itens(data):
        nome = it.get("name") or it.get("title") or ""
        preco = _wm_preco_item(it)
        if not nome or not preco:
            continue
        com_preco += 1
        if not _item_bate_com_busca(nome, query, exige):
            continue
        slug = it.get("canonicalUrl") or ""
        prod_url = ("https://www.walmart.com" + slug) if slug.startswith("/") else (slug or url)
        validos.append((preco, nome, prod_url))

    if not validos:
        print(f"      [WM] nenhum dos {com_preco} itens com preco e o produto certo")
        return None, None
    preco, nome, prod_url = min(validos)
    print(f"      [WM] '{nome[:60]}' ${preco} ({len(validos)} de {com_preco} itens batem)")
    return preco, prod_url

def fetch_walmart(query, exige=None):
    search_url = "https://www.walmart.com/search?q=" + requests.utils.quote(query)
    sc = make_scraper()
    try:
        r = sc.get(search_url, headers=hdrs(), timeout=30)
        print(f"      [WM] '{query[:40]}': HTTP {r.status_code}, {len(r.text)} bytes")
        if r.status_code == 200 and len(r.text) > 100000:
            return _parse_walmart_html(r.text, search_url, query, exige)
    except Exception as e:
        print(f"      [WM] erro: {e}")
    # ScraperAPI so se o acesso direto falhou (economiza a cota)
    r2 = scraperapi_get(search_url)
    if r2:
        print(f"      [WM] '{query[:40]}': ScraperAPI OK")
        return _parse_walmart_html(r2.text, search_url, query, exige)
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
            price, vid = fetch_bambulab(cfg["handle"], cfg.get("variant_hint"), nome=p.get("nome"),
                                         listar=cfg.get("listar", False))
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
                price = fetch_amazon(cfg["asin"], nome=p.get("nome"))
                url_produto = f"https://www.amazon.com/dp/{cfg['asin']}"
            else:
                price, url_produto = fetch_amazon_url(cfg.get("url",""))

        elif loja == "walmart":
            if "query" in cfg:
                price, found_url = fetch_walmart(cfg["query"], cfg.get("exige"))
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
            "url_produto": link_afiliado(url_produto),
            "url_carrinho": url_carrinho,
        }
        si = store_info(loja)
        status = f"${round(price,2)}" if price else "sem preco"
        print(f"    {si['emoji']} {si['nome']}: {status}")
        time.sleep(random.uniform(0.8, 1.5))

    # Limites plausíveis por categoria (evita preços claramente errados)
    _PRECO_MIN = {"impressora": 300, "acessorio": 5, "filamento": 5,
                  "fritadeira": 80, "eletronico": 20}
    min_plausivel = p.get("preco_min") or _PRECO_MIN.get(p.get("categoria", ""), 1)

    validos = {l: d["preco"] for l, d in item["lojas_precos"].items()
               if d.get("preco") and d["preco"] >= min_plausivel}
    if validos:
        melhor_loja  = min(validos, key=validos.get)
        melhor_preco = validos[melhor_loja]
        prev = item.get("preco_atual")
        item.update({"preco_atual": melhor_preco, "melhor_loja": melhor_loja})
        hist = item.get("historico", [])
        hist.append({"data": now, "preco": melhor_preco, "loja": melhor_loja})
        item["historico"]    = hist[-90:]
        item["preco_minimo"] = min(h["preco"] for h in item["historico"]
                                   if h["preco"] >= min_plausivel)
        if prev and melhor_preco < prev * 0.7:
            pct = (prev - melhor_preco) / prev * 100
            quedas_item.append(f"{p['nome']}: ${prev:.2f} -> ${melhor_preco:.2f} ({pct:.1f}% off)")
    else:
        item["preco_atual"] = None
        item["melhor_loja"] = None

    brasil_cfg = p.get("brasil")
    if brasil_cfg:
        preco_brl, url_brl, loja_nome_brl = None, "", ""
        ml_query  = brasil_cfg.get("ml_query")
        bl_handle = brasil_cfg.get("handle")
        bl_hint   = brasil_cfg.get("variant_hint")
        asin_br   = brasil_cfg.get("asin_br")

        # 1. Bambu Lab Brasil (loja oficial) — para produtos Bambu
        if not preco_brl and bl_handle:
            print(f"    [BR] Bambu Lab Brasil...")
            preco_brl, url_brl = fetch_bambulab_br(bl_handle, bl_hint)
            if preco_brl:
                loja_nome_brl = "Bambu Lab BR"

        # 2. Mercado Livre (menor preco novo)
        if not preco_brl and ml_query:
            print(f"    [BR] Mercado Livre...")
            preco_brl, url_brl = fetch_mercadolivre(ml_query)
            if preco_brl:
                loja_nome_brl = "Mercado Livre"

        # 3. Amazon.com.br
        if not preco_brl and (asin_br or ml_query):
            print(f"    [BR] Amazon.com.br...")
            preco_brl, url_brl = fetch_amazon_br(asin_br=asin_br, query=ml_query)
            if preco_brl:
                loja_nome_brl = "Amazon BR"

        # 4. Kabum (fallback)
        if not preco_brl and ml_query:
            print(f"    [BR] Kabum...")
            preco_brl, url_brl = fetch_kabum(ml_query)
            if preco_brl:
                loja_nome_brl = "Kabum"

        if preco_brl:
            item["brasil"] = {
                "preco_brl":  round(preco_brl, 2),
                "url":        link_afiliado(url_brl),
                "loja_nome":  loja_nome_brl,
            }
            print(f"    Brasil ({loja_nome_brl}): R${preco_brl:.2f}")
        else:
            item.setdefault("brasil", None)

    return quedas_item

# ---------------------------------------------------------------------------
# Alerta de queda de preco: escreve compras/alerta.md; o workflow transforma
# em issue no GitHub, que avisa o dono por e-mail e no app do celular.
# ---------------------------------------------------------------------------
ORLANDO_TAX_ALERTA = 0.065   # imposto de venda de Orlando

def _config_alertas():
    cfg = {"queda_minima_pct": 10, "precos_alvo_usd": {}}
    try:
        with open(ALERTAS_CFG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [Alerta] alertas.json invalido: {e}")
    return cfg

def _usd(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def gerar_alertas(data, anteriores, now, usd_brl):
    """Compara o preco atual com o da atualizacao anterior e com o preco-alvo.
    Nao repete aviso para o mesmo preco: so avisa de novo se cair ainda mais,
    ou depois que o preco subir e voltar a cair."""
    for caminho in (ALERTA_CORPO, ALERTA_TITULO):
        if os.path.exists(caminho):
            os.remove(caminho)
    cfg = _config_alertas()
    queda_min = float(cfg.get("queda_minima_pct") or 10)
    alvos = cfg.get("precos_alvo_usd") or {}
    enviados = data.setdefault("alertas_enviados", {})
    fator_brl = usd_brl * (1 + ORLANDO_TAX_ALERTA) * 1.04

    avisos = []
    for pid, item in data.get("items", {}).items():
        atual = item.get("preco_atual")
        if not atual:
            continue
        ult = enviados.get(pid)
        if ult and atual > ult["preco"] * 1.02:     # subiu de novo: libera novo aviso
            del enviados[pid]
            ult = None

        motivos = []
        prev = anteriores.get(pid)
        if prev and atual < prev:
            pct = (prev - atual) / prev * 100
            if pct > 60:
                print(f"  [Alerta] {item.get('nome','')[:40]}: queda de {pct:.0f}% parece erro de leitura, ignorada")
                continue                              # nem o preco-alvo vale com leitura suspeita
            if pct >= queda_min:
                motivos.append(f"caiu {pct:.0f}% (era US$ {_usd(prev)})")
        alvo = alvos.get(pid)
        if alvo and atual <= float(alvo):
            motivos.append(f"chegou ao seu preco-alvo de US$ {_usd(float(alvo))}")
        if not motivos:
            continue
        if ult and atual >= ult["preco"] - 0.009:    # ja avisado neste preco
            continue

        enviados[pid] = {"preco": atual, "data": now}
        loja = item.get("melhor_loja") or ""
        url = (item.get("lojas_precos", {}).get(loja) or {}).get("url_produto") or ""
        avisos.append({"nome": item.get("nome", pid), "preco": atual, "loja": store_info(loja)["nome"],
                       "url": url, "motivos": motivos, "minimo": item.get("preco_minimo"),
                       "brl": atual * fator_brl})

    if not avisos:
        print("  [Alerta] nenhuma queda para avisar")
        return []

    dono = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if len(avisos) == 1:
        titulo = f"Queda de preco: {avisos[0]['nome'][:60]} por US$ {_usd(avisos[0]['preco'])}"
    else:
        titulo = f"Queda de preco em {len(avisos)} produtos"
    linhas = [f"@{dono} " if dono else "", "Encontrei quedas de preco na atualizacao de hoje:\n"]
    for a in avisos:
        link = f"[{a['loja']}]({a['url']})" if a["url"] else a["loja"]
        linhas.append(f"### {a['nome']}")
        brl = f"{a['brl']:,.0f}".replace(",", ".")
        linhas.append(f"- **US$ {_usd(a['preco'])}** em {link} (cerca de R$ {brl} com imposto e cartao)")
        linhas.append(f"- {'; '.join(a['motivos'])}")
        if a["minimo"]:
            linhas.append(f"- menor preco ja visto: US$ {_usd(a['minimo'])}")
        linhas.append("")
    linhas.append("---\nAjuste o percentual e os precos-alvo em `compras/alertas.json`. "
                  "Pode fechar esta issue depois de ver.")
    with open(ALERTA_CORPO, "w") as f:
        f.write("\n".join(linhas))
    with open(ALERTA_TITULO, "w") as f:
        f.write(titulo)
    print(f"  [Alerta] {len(avisos)} aviso(s): {titulo}")
    return avisos

def main():
    print(f"\n  BESTBUY_API_KEY: {'configurado' if BESTBUY_API_KEY else 'NAO configurado'}")
    print(f"  SCRAPER_API_KEY: {'configurado' if SCRAPER_API_KEY else 'NAO configurado'}")
    print(f"  ML_APP_ID:       {'configurado' if ML_APP_ID else 'NAO configurado'}")
    print(f"  ML_SECRET_KEY:   {'configurado' if ML_SECRET_KEY else 'NAO configurado'}")
    print(f"  ANTHROPIC_API_KEY: {'configurado (' + CLAUDE_MODEL + ')' if ANTHROPIC_API_KEY else 'NAO configurado'}")

    data = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)

    now = datetime.now(timezone.utc).isoformat()
    data["atualizado_em"] = now
    data.setdefault("items", {})

    print("\n=== Buscando cambio ===")
    usd_brl, fonte_cambio, data_cambio = fetch_brl_usd(data.get("cambio"))
    data["cambio"] = {
        "usd_brl":      round(usd_brl, 4),
        "fonte":        fonte_cambio,
        "atualizado_em": data_cambio,
    }
    quedas = []
    ids_processados = set()
    anteriores = {pid: it.get("preco_atual") for pid, it in data["items"].items()}

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

    close_browser()

    print("\n=== Alertas de queda de preco ===")
    gerar_alertas(data, anteriores, now, usd_brl)

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
