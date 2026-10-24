"""PA announcements in English, Hindi and Kannada.

Text comes from the templates below. Audio comes from gTTS (needs internet) and is
cached in ./pa_cache. If gTTS fails, the web page falls back to the browser's own voice.

Ask a Hindi and a Kannada speaker on the team to read these once before the demo.
"""
import asyncio
import hashlib
import os
import tempfile

LANGS = ("en", "hi", "kn")
LANG_NAMES = {"en": "English", "hi": "हिन्दी", "kn": "ಕನ್ನಡ"}

TEMPLATES = {
    "crowd": {
        "en": "Attention please. {zone} is very crowded. Please move slowly towards {target}.",
        "hi": "कृपया ध्यान दें। {zone} क्षेत्र में बहुत भीड़ है। कृपया धीरे-धीरे {target} की ओर जाएँ।",
        "kn": "ದಯವಿಟ್ಟು ಗಮನಿಸಿ. {zone} ಪ್ರದೇಶದಲ್ಲಿ ಹೆಚ್ಚು ಜನಸಂದಣಿ ಇದೆ. ದಯವಿಟ್ಟು ನಿಧಾನವಾಗಿ {target} ಕಡೆಗೆ ಹೋಗಿ.",
    },
    "crowd_stay": {
        "en": "Attention please. {zone} is very crowded. Please wait where you are and follow the staff's instructions.",
        "hi": "कृपया ध्यान दें। {zone} क्षेत्र में बहुत भीड़ है। कृपया जहाँ हैं वहीं रुकें और कर्मचारियों के निर्देशों का पालन करें।",
        "kn": "ದಯವಿಟ್ಟು ಗಮನಿಸಿ. {zone} ಪ್ರದೇಶದಲ್ಲಿ ಹೆಚ್ಚು ಜನಸಂದಣಿ ಇದೆ. ದಯವಿಟ್ಟು ಇದ್ದಲ್ಲೇ ನಿಲ್ಲಿ ಮತ್ತು ಸಿಬ್ಬಂದಿಯ ಸೂಚನೆಗಳನ್ನು ಪಾಲಿಸಿ.",
    },
    "make_way": {
        "en": "Attention please. Please make way for the {team} team at {zone}. Do not gather around the area.",
        "hi": "कृपया ध्यान दें। {zone} क्षेत्र में {team} टीम को रास्ता दें। वहाँ भीड़ न लगाएँ।",
        "kn": "ದಯವಿಟ್ಟು ಗಮನಿಸಿ. {zone} ಪ್ರದೇಶದಲ್ಲಿ {team} ತಂಡಕ್ಕೆ ದಾರಿ ಮಾಡಿಕೊಡಿ. ಅಲ್ಲಿ ಗುಂಪು ಸೇರಬೇಡಿ.",
    },
    "calm": {
        "en": "Attention please. Please stay calm and walk slowly. Follow the staff's instructions.",
        "hi": "कृपया ध्यान दें। कृपया शांत रहें और धीरे चलें। कर्मचारियों के निर्देशों का पालन करें।",
        "kn": "ದಯವಿಟ್ಟು ಗಮನಿಸಿ. ದಯವಿಟ್ಟು ಶಾಂತವಾಗಿರಿ ಮತ್ತು ನಿಧಾನವಾಗಿ ನಡೆಯಿರಿ. ಸಿಬ್ಬಂದಿಯ ಸೂಚನೆಗಳನ್ನು ಪಾಲಿಸಿ.",
    },
    "tracks": {
        "en": "Attention please. Please stay behind the platform edge. Do not cross the tracks.",
        "hi": "कृपया ध्यान दें। कृपया प्लेटफ़ॉर्म के किनारे से पीछे रहें। पटरियाँ पार न करें।",
        "kn": "ದಯವಿಟ್ಟು ಗಮನಿಸಿ. ದಯವಿಟ್ಟು ಪ್ಲಾಟ್‌ಫಾರ್ಮ್ ಅಂಚಿನಿಂದ ಹಿಂದೆ ನಿಲ್ಲಿ. ಹಳಿಗಳನ್ನು ದಾಟಬೇಡಿ.",
    },
}

ALL_CLEAR = {
    "en": "Update: {zone} is clear now. Thank you for your patience.",
    "hi": "सूचना: {zone} क्षेत्र में अब भीड़ कम है। धैर्य रखने के लिए धन्यवाद।",
    "kn": "ಮಾಹಿತಿ: {zone} ಪ್ರದೇಶದಲ್ಲಿ ಈಗ ಜನಸಂದಣಿ ಕಡಿಮೆಯಾಗಿದೆ. ತಾಳ್ಮೆಯಿಂದ ಇದ್ದುದಕ್ಕೆ ಧನ್ಯವಾದಗಳು.",
}

TEAMS = {
    "medical":  {"en": "medical", "hi": "चिकित्सा", "kn": "ವೈದ್ಯಕೀಯ"},
    "security": {"en": "security", "hi": "सुरक्षा", "kn": "ಭದ್ರತಾ"},
    "police":   {"en": "police", "hi": "पुलिस", "kn": "ಪೊಲೀಸ್"},
    "rpf":      {"en": "railway police", "hi": "रेलवे पुलिस", "kn": "ರೈಲ್ವೆ ಪೊಲೀಸ್"},
}


def _name(entry, lang):
    """A zone or exit name in the given language, falling back to English."""
    return (entry.get("names") or {}).get(lang) or entry.get("name") or ""


def build(template, zone_cfg, redirect=None, team_kind=None):
    """Return {"en": ..., "hi": ..., "kn": ...} or None."""
    if template is None:
        return None
    if template == "crowd" and not redirect:
        template = "crowd_stay"
    texts = {}
    for lang in LANGS:
        texts[lang] = TEMPLATES[template][lang].format(
            zone=_name(zone_cfg, lang),
            target=_name(redirect, lang) if redirect else "",
            team=TEAMS.get(team_kind or "security", TEAMS["security"])[lang],
        )
    return texts


class Voice:
    def __init__(self, cache_dir="pa_cache"):
        self.available = None   # None = not tried yet, True/False after first attempt
        self.last_error = None
        self.cache_dir = None
        # Windows can block creating folders inside the project (Controlled folder access, read-only
        # folders). Fall back to the system temp folder, and if even that fails, use the browser's voice.
        for d in (cache_dir, os.path.join(tempfile.gettempdir(), "poltergeist_pa_cache")):
            try:
                os.makedirs(d, exist_ok=True)
                probe = os.path.join(d, ".write_test")
                with open(probe, "w") as f:
                    f.write("ok")
                os.remove(probe)
                self.cache_dir = d
                break
            except OSError:
                continue
        if self.cache_dir is None:
            self.available = False
            self.last_error = "No folder to save voice files, so the browser's voice is used."
        elif self.cache_dir != cache_dir:
            print(f"  Voice files go to {self.cache_dir} (couldn't write to {cache_dir}).")

    def path_for(self, lang, text):
        key = hashlib.sha1(f"{lang}|{text}".encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.cache_dir, f"{lang}-{key}.mp3")

    def _make(self, lang, text, path):
        from gtts import gTTS  # imported lazily so the server still runs without it
        tmp = path + ".part"
        gTTS(text=text, lang=lang, tld="co.in" if lang == "en" else "com").save(tmp)
        os.replace(tmp, path)

    async def mp3(self, lang, text):
        """Path to an mp3 for this text, or None if gTTS can't make one right now."""
        if lang not in LANGS or not text or self.cache_dir is None:
            return None
        path = self.path_for(lang, text)
        if os.path.exists(path):
            return path
        try:
            await asyncio.wait_for(asyncio.to_thread(self._make, lang, text, path), timeout=12)
            self.available = True
            return path
        except Exception as e:  # no internet, gTTS missing, Google blocked...
            self.available = False
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return None

    async def prewarm(self, texts):
        if not texts:
            return
        for lang in LANGS:
            await self.mp3(lang, texts.get(lang))
