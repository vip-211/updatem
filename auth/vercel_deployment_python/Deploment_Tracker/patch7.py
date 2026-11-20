#!/usr/bin/env python3
"""
Patch v2: Git links are optional when the ITSM category is one of
Cloud, DWH, IVR, Linux, Middleware, Network, Nutanix, Storage.

Targets the CURRENT Release_Calender.html (uses allowedCabDates array).

Usage (run from the project folder):
    python apply_git_optional_patch_v2.py
    python apply_git_optional_patch_v2.py path/app.py path/Release_Calender.html

Defaults: app.py and templates/Release_Calender.html
- Checks BOTH files first and lists every block that does not match.
- Writes nothing unless everything matches.
- Makes .bak backups before writing.
- Skips a file that is already patched.
Restart Flask and hard-refresh (Ctrl+F5) afterwards.
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


def apply_edits(src, edits, failures):
    for label, old, new in edits:
        matches = list(re.finditer(flex(old), src, flags=re.S))
        if len(matches) != 1:
            first = old.strip().splitlines()[0].strip()
            lines = [i + 1 for i, l in enumerate(src.splitlines()) if first in l]
            failures.append((label, len(matches), first, lines))
            continue
        m = matches[0]
        src = src[:m.start()] + new + src[m.end():]
    return src


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
# Release_Calender.html (current version: allowedCabDates array)
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
        "html: checkCRNumber success (after CAB dates load)",
        '''allowedCabDates = result.cab_dates.map(d => d.trim());''',
        '''allowedCabDates = result.cab_dates.map(d => d.trim());

        // Git links are optional for certain ITSM categories.
        gitOptional = isGitOptional(result.category);
        applyGitRequirement();''',
    ),
    (
        "html: checkCRNumber catch",
        '''lookupFailed = true;
        allowedCabDates = [];''',
        '''lookupFailed = true;
        gitOptional = false; applyGitRequirement();
        allowedCabDates = [];''',
    ),
    (
        "html: CR input handler reset",
        '''lookupFailed = false;
allowedCabDates = [];''',
        '''lookupFailed = false;
gitOptional = false; applyGitRequirement();
allowedCabDates = [];''',
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

    app_done = "GIT_OPTIONAL_CATEGORIES" in a
    html_done = "GIT_OPTIONAL_CATEGORIES" in h

    failures = []
    new_a = a if app_done else apply_edits(a, APP_EDITS, failures)
    new_h = h if html_done else apply_edits(h, HTML_EDITS, failures)

    if failures:
        print(f"{len(failures)} block(s) do not match. NOTHING was written.\n")
        for label, n, first, lines in failures:
            print(f"- {label}")
            print(f"    matches found : {n} (needs exactly 1)")
            print(f"    block starts  : {first[:90]}")
            print(f"    at line(s)    : {lines if lines else 'NOT FOUND in file'}\n")
        print("Send me this output and I will adjust the patch.")
        sys.exit(1)

    if app_done and html_done:
        print("Both files are already patched. Nothing to do.")
        return

    if not app_done:
        shutil.copy2(app_py, str(app_py) + ".bak")
        app_py.write_text(new_a, encoding="utf-8")
        print(f"Patched {app_py} (backup: {app_py}.bak)")
    else:
        print(f"{app_py}: already patched, skipped.")

    if not html_done:
        shutil.copy2(html, str(html) + ".bak")
        html.write_text(new_h, encoding="utf-8")
        print(f"Patched {html} (backup: {html}.bak)")
    else:
        print(f"{html}: already patched, skipped.")

    print("Done. Restart Flask and hard-refresh (Ctrl+F5).")


if __name__ == "__main__":
    main()