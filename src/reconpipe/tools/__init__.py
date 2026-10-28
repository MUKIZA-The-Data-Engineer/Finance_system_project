"""The ReconPipe toolkit: each stage of the pipeline as a tool you can use on its own.

    reconpipe pdf-to-excel   any PDF bank statement -> Excel
    reconpipe standardize    any messy Excel/CSV/PDF -> clean, standard Excel + issues
    reconpipe reconcile      any two files (e.g. bank vs books) -> reconciliation workbook
    reconpipe odoo-export    any file -> Odoo-ready import file, validated
    reconpipe ai             prompt library and AI column mapping (Claude)

The tools reuse the pipeline's own layers (parsers, cleaning, validation), so a
file gives the same answer whether it goes through a tool or the full pipeline.
"""
