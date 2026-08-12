"""Playwright evasion: launch arguments and an init script.

Scope note: these evasions normalize *automation give-aways* — properties that
differ between a headless automation build and the same browser driven by a
person. They do not solve CAPTCHAs, do not compute proof-of-work, and do not
target any specific vendor. When a challenge is detected, scrapeforge raises
:class:`~scrapeforge.exceptions.ChallengeError` and lets the caller decide.
"""

from __future__ import annotations

from .user_agents import UserAgentProfile

__all__ = ["BROWSER_TYPE_TO_FAMILY", "STEALTH_SCRIPT", "build_init_script", "launch_args"]

#: Which user-agent family is coherent with each Playwright engine. Claiming to
#: be Firefox while running Chromium is a stronger signal than any header
#: mismatch — the JS engine surface gives it away immediately.
BROWSER_TYPE_TO_FAMILY: dict[str, str] = {
    "chromium": "chrome",
    "firefox": "firefox",
    "webkit": "safari",
}

#: Chromium-only shim. ``window.chrome`` is absent in headless Chromium, and must
#: not be present at all on Firefox or WebKit.
_CHROME_OBJECT_SCRIPT = """
(() => {
  if (!window.chrome) { window.chrome = {}; }
  if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  if (!window.chrome.csi) { window.chrome.csi = () => ({}); }
  if (!window.chrome.loadTimes) { window.chrome.loadTimes = () => ({}); }
})();
"""

#: Launch flags that remove obvious automation markers and headless-only quirks.
_BASE_LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)

#: Base evasions, independent of the chosen profile.
STEALTH_SCRIPT: str = """
(() => {
  const def = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, { get: () => value, configurable: true });
    } catch (_) { /* property is locked down; nothing to do */ }
  };

  // navigator.webdriver is the single most-checked automation flag.
  def(Navigator.prototype, 'webdriver', undefined);
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (_) {}

  // Headless builds report an empty plugin/mimeType list.
  const makePlugin = (name, filename, description) => {
    const plugin = Object.create(Plugin.prototype);
    def(plugin, 'name', name);
    def(plugin, 'filename', filename);
    def(plugin, 'description', description);
    def(plugin, 'length', 1);
    return plugin;
  };
  const plugins = [
    makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    makePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
  ];
  Object.setPrototypeOf(plugins, PluginArray.prototype);
  def(Navigator.prototype, 'plugins', plugins);

  const mimeTypes = [];
  Object.setPrototypeOf(mimeTypes, MimeTypeArray.prototype);
  def(Navigator.prototype, 'mimeTypes', mimeTypes);

  // Permissions.query returns 'denied' for notifications in headless, but
  // 'prompt' in a real profile with default settings.
  if (window.Notification && navigator.permissions) {
    const original = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) =>
      params && params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : original(params);
  }

  // Consistent WebGL vendor/renderer. Values must look like a real GPU, and
  // must not change between calls within a session.
  const patchWebGL = (proto) => {
    if (!proto) return;
    const getParameter = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) return 'Intel Inc.';            // UNMASKED_VENDOR_WEBGL
      if (parameter === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
      return getParameter.apply(this, arguments);
    };
  };
  patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  // Headless reports 0 for these; real machines never do.
  if (!navigator.maxTouchPoints && navigator.maxTouchPoints !== 0) {
    def(Navigator.prototype, 'maxTouchPoints', 0);
  }
})();
"""


def launch_args(*, extra: list[str] | None = None) -> list[str]:
    """Return Chromium launch arguments that avoid automation give-aways.

    Args:
        extra: Additional flags appended after the defaults.
    """
    args = list(_BASE_LAUNCH_ARGS)
    if extra:
        args.extend(extra)
    return args


def build_init_script(
    profile: UserAgentProfile, *, languages: tuple[str, ...] = ("en-US", "en")
) -> str:
    """Build an init script tailored to ``profile``.

    Values injected here (``platform``, ``hardwareConcurrency``, ``deviceMemory``,
    ``languages``) must agree with the ``User-Agent`` and viewport the context is
    configured with — a mismatch is itself a signal.

    Args:
        profile: The profile the browser context is being configured with.
        languages: Value for ``navigator.languages``.

    Returns:
        JavaScript to pass to ``context.add_init_script``.
    """
    platform_map = {
        "Windows": "Win32",
        "macOS": "MacIntel",
        "Linux": "Linux x86_64",
        "Android": "Linux armv8l",
        "iOS": "iPhone",
    }
    nav_platform = platform_map.get(profile.platform, "Win32")
    cores = 4 if profile.mobile else 8
    memory = 4 if profile.mobile else 8
    touch_points = 5 if profile.mobile else 0

    profile_script = f"""
(() => {{
  const def = (obj, prop, value) => {{
    try {{
      Object.defineProperty(obj, prop, {{ get: () => value, configurable: true }});
    }} catch (_) {{}}
  }};
  def(Navigator.prototype, 'platform', {nav_platform!r});
  def(Navigator.prototype, 'languages', {list(languages)!r});
  def(Navigator.prototype, 'hardwareConcurrency', {cores});
  def(Navigator.prototype, 'maxTouchPoints', {touch_points});
}})();
"""
    script = STEALTH_SCRIPT + profile_script
    if profile.is_chromium:
        # navigator.deviceMemory and window.chrome exist only in Chromium.
        # Defining them on a Firefox or WebKit profile would manufacture exactly
        # the inconsistency these evasions exist to remove.
        script += f"""
(() => {{
  try {{
    Object.defineProperty(Navigator.prototype, 'deviceMemory',
      {{ get: () => {memory}, configurable: true }});
  }} catch (_) {{}}
}})();
"""
        script += _CHROME_OBJECT_SCRIPT
    return script
