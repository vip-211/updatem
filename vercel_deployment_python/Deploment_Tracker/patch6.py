#!/usr/bin/env python3
"""
Patch: Git links are optional when the ITSM category is one of
Cloud, DWH, IVR, Linux, Middleware, Network, Nutanix, Storage.

Usage (run from the project folder):
    python apply_git_optional_patch.py
    python apply_git_optional_patch.py path/app.py path/Release_Calender.html

Defaults: app.py and templates/Release_Calender.html
Creates .bak backups first. Restart Flask and hard-refresh (Ctrl+F5) afterwards.
"""
import re
import shutil
import sys
from pathlib import Path

app_py = Path(sys.argv[1] if len(sys.argv) > 1 else "app.py")
html = Path(sys.argv[2] if len(sys.argv) > 2 else "templates/Release_Calender.html")


def flex(text):
    """Regex matching `text` regardless of whitespace/indentation differences."""
    return r"\s+".join(re.escape(tok) for tok in text.split())


def sub_once(src, old, new, label):
    matches = list(re.finditer(flex(old), src, flags=re.S))
    if len(matches) != 1:
        sys.exit(f"[FAILED] {label}: expected 1 match, found {len(matches)}. Nothing was written.")
    m = matches[0]
    return src[:m.start()] + new + src[m.end():]


# ----------------------------------------------------------------------------
# app.py
# ----------------------------------------------------------------------------
APP_EDITS = [
    (
        "app.py: category list + helper",
        'DEPLOYMENT_TYPES = ["Automation", "Manual"]',
        '''DEPLOYMENT_TYPES = ["Automation", "Manual"]

# ITSM categories for which Git repository links are optional.
GIT_OPTIONAL_CATEGORIES = {
    "cloud", "dwh", "ivr", "linux",
    "middleware", "network", "nutanix", "storage",
}


def is_git_optional(category):
    return (category or "").strip().casefold() in GIT_OPTIONAL_CATEGORIES''',
    ),
    (
        "app.py: save_deployment git link validation",
        '''git_links = request.form.getlist("git_links[]")
    if not git_links:
        raise ValueError("At least one Git link is required.")

    for link in git_links:

        if not validate_git_link(link):
            raise ValueError(
                f"Invalid Git link: {link}"
            )''',
        '''git_links = [
        link.strip()
        for link in request.form.getlist("git_links[]")
        if link and link.strip()
    ]
    if not git_links and not is_git_optional(category):
        raise ValueError("At least one Git link is required.")

    for link in git_links:
        if not validate_git_link(link):
            raise ValueError(f"Invalid Git link: {link}")''',
    ),
    (
        "app.py: deployment email git links",
        '''f"Git Links: {', '.join(git_links)}",''',
        '''f"Git Links: {', '.join(git_links) or 'Not provided'}",''',
    ),
]

# ----------------------------------------------------------------------------
# Release_Calender.html
# ----------------------------------------------------------------------------
HTML_EDITS = [
    (
        "html: label + hint",
        '''<label>Git Links <span aria-hidden="true" style="color:#dc2626;font-weight:700;">*</span><span class="sr-only"> (required)</span></label>''',
        '''<label>Git Links <span id="gitRequiredStar" aria-hidden="true" style="color:#dc2626;font-weight:700;">*</span><span class="sr-only" id="gitRequiredSr"> (required)</span></label>
                        <span class="hint" id="gitLinkHint"></span>''',
    ),
    (
        "html: helpers",
        '''const slotCapacitySummary = document.getElementById("slotCapacitySummary");''',
        '''const slotCapacitySummary = document.getElementById("slotCapacitySummary");

            // Git links are optional for these ITSM categories.
            const GIT_OPTIONAL_CATEGORIES = ["cloud", "dwh", "ivr", "linux", "middleware", "network", "nutanix", "storage"];
            let gitOptional = false;

            function isGitOptional(category) {
                return GIT_OPTIONAL_CATEGORIES.includes(String(category || "").trim().toLowerCase());
            }

            function applyGitRequirement() {
                const required = !gitOptional;
                document.querySelectorAll("#git-links-container .git-link").forEach(function (input) {
                    input.required = required;
                    if (!required) input.setCustomValidity("");
                });
                const star = document.getElementById("gitRequiredStar");
                const sr = document.getElementById("gitRequiredSr");
                const hint = document.getElementById("gitLinkHint");
                if (star) star.style.display = required ? "" : "none";
                if (sr) sr.textContent = required ? " (required)" : " (optional)";
                if (hint) hint.textContent = required ? "" : "Git links are optional for this category.";
            }''',
    ),
    (
        "html: validateGitLink empty allowed",
        '''const value = input.value.trim();''',
        '''const value = input.value.trim();
                if (!value && gitOptional) { input.setCustomValidity(""); return true; }''',
    ),
    (
        "html: validateGitLinkCount",
        '''function validateGitLinkCount() {''',
        '''function validateGitLinkCount() {
                if (gitOptional) return true;''',
    ),
    (
        "html: checkCRNumber success",
        '''allowedCabDate = result.cab_date_iso;''',
        '''allowedCabDate = result.cab_date_iso;
                    gitOptional = isGitOptional(result.category);
                    applyGitRequirement();''',
    ),
    (
        "html: checkCRNumber catch",
        '''lookupFailed = true;
                    allowedCabDate = "";''',
        '''lookupFailed = true;
                    gitOptional = false; applyGitRequirement();
                    allowedCabDate = "";''',
    ),
    (
        "html: CR input handler reset",
        '''lookupFailed = false;
                allowedCabDate = "";''',
        '''lookupFailed = false;
                gitOptional = false; applyGitRequirement();
                allowedCabDate = "";''',
    ),
    (
        "html: addGitLink new rows",
        '''container.appendChild(row);
                updateGitAddButton();''',
        '''container.appendChild(row);
                applyGitRequirement();
                updateGitAddButton();''',
    ),
]


def main():
    for p in (app_py, html):
        if not p.exists():
            sys.exit(f"File not found: {p}")

    a = app_py.read_text(encoding="utf-8")
    h = html.read_text(encoding="utf-8")

    if "GIT_OPTIONAL_CATEGORIES" in a and "GIT_OPTIONAL_CATEGORIES" in h:
        print("Already patched. Nothing to do.")
        return

    for label, old, new in APP_EDITS:
        a = sub_once(a, old, new, label)
    for label, old, new in HTML_EDITS:
        h = sub_once(h, old, new, label)

    shutil.copy2(app_py, str(app_py) + ".bak")
    shutil.copy2(html, str(html) + ".bak")
    app_py.write_text(a, encoding="utf-8")
    html.write_text(h, encoding="utf-8")
    print("Patched app.py and Release_Calender.html (backups: *.bak). Restart Flask.")


if __name__ == "__main__":
    main()