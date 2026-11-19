# GitHub build manifest

The same as `../com.charleshartmann.Simmer.json` (what Depot builds), plus one line that marks the package as a GitHub download
(`/app/share/com.charleshartmann.Simmer/channel` = `github`). Bundles attached to GitHub releases are built from this manifest, so
Simmer can offer the next GitHub release, verify it against `SHA256SUMS`, and open it with the system's
installer. Depot builds use the main manifest and get updates from Depot only (Luma review 1.3).
