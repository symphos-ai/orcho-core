Review the cross-project contract bundle supplied by the runner.

Use the bundle's verification checklist as the acceptance bar.
Identify concrete cross-project contract findings only:

- mismatched producer/consumer field names, types, requiredness, or payload shapes;
- persisted shape gaps that would make one project write data another project cannot read;
- hardcoded constants, event names, IDs, or schema versions that drift across aliases;
- missing per-alias evidence when the bundle claims a coordinated change is safe.

Do not inspect the working tree. The bundle is the review target.
