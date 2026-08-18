"""Rendu de pages billetterie via Playwright (navigateur headless).

Utilisé pour les billetteries dont la grille tarifaire est chargée en
JavaScript et que le simple `web_fetch` ne voit pas. On rend la page dans
Chromium, on gère les bandeaux de consentement, et on extrait le texte visible
que l'on transmet ensuite au LLM pour structuration.

Chromium est pré-installé dans l'environnement (voir `PLAYWRIGHT_BROWSERS_PATH`);
`playwright install` n'est pas nécessaire.
"""

from __future__ import annotations

import glob
import os
from typing import Optional

from . import config


def _chromium_executable() -> Optional[str]:
    """Localise l'exécutable Chromium pré-installé, s'il existe."""
    override = os.environ.get("BENCHMARKER_CHROMIUM_PATH")
    if override and os.path.exists(override):
        return override
    patterns = [
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium/chrome-linux/chrome",
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None  # laisse Playwright utiliser son navigateur par défaut


class PageRenderer:
    """Rendu de pages avec un navigateur réutilisé sur plusieurs URLs.

    À utiliser comme context manager :

        with PageRenderer() as r:
            html_text = r.render(url)
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        use_proxy: bool = True,
        nav_timeout_ms: int = config.RENDER_TIMEOUT_MS,
        text_cap: int = config.RENDER_TEXT_CAP,
    ) -> None:
        self.headless = headless
        self.use_proxy = use_proxy
        self.nav_timeout_ms = nav_timeout_ms
        self.text_cap = text_cap
        self._pw = None
        self._browser = None

    # --- cycle de vie -------------------------------------------------------
    def __enter__(self) -> "PageRenderer":
        from playwright.sync_api import sync_playwright  # import tardif

        self._pw = sync_playwright().start()
        launch_kwargs: dict = {
            "headless": self.headless,
            # --disable-http2 : certains sites (billetteries incluses) coupent
            # la connexion (ERR_HTTP2_PROTOCOL_ERROR) face à un navigateur
            # headless en HTTP/2 ; HTTP/1.1 contourne le problème sans coût
            # notable pour ce cas d'usage (une page à la fois).
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-http2"],
        }
        exe = _chromium_executable()
        if exe:
            launch_kwargs["executable_path"] = exe
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if self.use_proxy and proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        return self

    def __exit__(self, *exc) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            finally:
                self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    # --- rendu --------------------------------------------------------------
    def render(self, url: str) -> str:
        """Rend une URL et renvoie le texte visible (plafonné).

        Retente une fois sur une erreur réseau/protocole (ex.
        ERR_HTTP2_PROTOCOL_ERROR, connexion coupée par une protection anti-bot
        intermittente) avec un contexte frais. Lève l'exception finale si
        toutes les tentatives échouent ; l'appelant décide quoi faire ensuite
        (fallback web_fetch, note de confiance, etc.).
        """
        if self._browser is None:
            raise RuntimeError("PageRenderer doit être utilisé comme context manager.")

        last_exc: Optional[Exception] = None
        for attempt in range(config.RENDER_MAX_ATTEMPTS):
            context = self._browser.new_context(
                ignore_https_errors=True,
                locale="fr-FR",
                user_agent=config.RENDER_USER_AGENT,
                extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
            )
            # Réduit le signal le plus évident de navigateur headless : certains
            # sites bloquent agressivement sur ce seul indicateur, ce qui peut
            # se traduire par une coupure réseau plutôt qu'un simple refus HTTP.
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
                self._dismiss_cookie_banner(page)
                # Laisse le temps au contenu JS (grilles tarifaires) de se charger.
                try:
                    page.wait_for_load_state("networkidle", timeout=config.RENDER_IDLE_MS)
                except Exception:
                    pass  # networkidle peut ne jamais arriver (trackers) — pas bloquant
                text = page.inner_text("body")
                return text[: self.text_cap]
            except Exception as exc:
                last_exc = exc
                is_network_error = "net::" in str(exc)
                if not is_network_error or attempt == config.RENDER_MAX_ATTEMPTS - 1:
                    raise
                # Sinon : nouvelle tentative avec un contexte frais.
            finally:
                context.close()

        raise last_exc  # pragma: no cover — sécurité, la boucle raise déjà au dernier essai

    def _dismiss_cookie_banner(self, page) -> None:
        """Tente de fermer un bandeau de consentement (best effort)."""
        for selector in config.COOKIE_ACCEPT_SELECTORS:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click(timeout=1500)
                    page.wait_for_timeout(300)
                    return
            except Exception:
                continue


def render_page(url: str, **kwargs) -> str:
    """Raccourci : rend une seule URL (ouvre/ferme un navigateur dédié)."""
    with PageRenderer(**kwargs) as renderer:
        return renderer.render(url)
