# H7123 and H712C model evidence

These observations were made on 2026-09-05 using local Home Assistant 2026.9.0.
Addresses, household names, session secrets and raw device metadata are omitted.
Only application status/command vectors needed for reproducible tests are kept.
Both profiles remain experimental; other firmware revisions are untested.

## H712C

Two physical H712C units were identified by their advertised model name and
independently matched to their device records. Plaintext queries returned:

- Low mode: `aa 05 00 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 af`.
- Turbo mode: `aa 05 00 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 a8`.

Manual level commands use `33 05 01 01`, `33 05 01 02`, and `33 05 01 03`
(low/medium/high), zero-padded to 19 bytes followed by their XOR checksum.
Sleep and Turbo use mode codes `05` and `07`. Power, Medium, Sleep and Turbo
were tested on one unit; Medium was tested on the second. Original speeds were
restored and verified through a fresh connection/readback. High's command vector
is covered by tests and the reference handler, but was not physically exercised.

The [official H712C manual](https://govee-oss-public.oss-us-east-1.aliyuncs.com/prod/permanent-directory/skuDescriptionFile/H712C.pdf)
shows Sleep, three manual levels and Turbo, without Auto. The profile exposes
five levels at 20/40/60/80/100%, and rejects Auto. No unverified night-light,
PM2.5, filter-life or Custom Auto entities are exposed.

## H7123

Plaintext status queries produced no replies. A fresh encrypted session using
the existing H7129 session mechanism succeeded. Both `aa 05 00` and `aa 05 01`
queries returned the direct Medium mode report:

`aa 05 01 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ac`.

Unlike H712C, byte 2 is the mode code and byte 3 is the manual level; it is not
a selector-00 startup report. A matching direct report can confirm the requested
fan mode; a report for a different mode must not acknowledge that command.

Commands use `3a 05`. Sleep/Low/Medium/High map to 25/50/75/100%; Auto is
separate and Turbo is rejected. Low, Sleep, High, Auto and power off/on were
physically exercised, with fresh readback and original Medium restored.

The observed canonical Auto echo was
`3a 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 28`.
The reference handler's Auto parameter zero did not match this unit; parameter
`14` was then verified through command and reconnect/readback.

Standard initialization requests `33 b2` and `33 b5` precede power/mode queries.
The integration retains strict acknowledgement rules and only polls `aa 01`.
An experiment tolerating idle poll failures did not prevent disconnects and was
reverted; it is not included in this contribution.

## Unresolved stability and limits

Initial H7123 sessions missed repeated idle polls. A separate adapter-wide HCI
failure affected all configured purifiers during validation. Host-side USB reset
and passthrough reattachment recovered the adapter. These host operations are
not part of this integration or its tests.

With the final initialization sequence and recovered adapter, H7123 passed a
60-second idle observation and a subsequent speed/reconnect/readback test.
Another session later missed polls and disconnected, then recovered and passed
another observation. This is evidence of intermittent failure, not a complete
stability fix. The contribution cannot isolate initialization changes from
adapter recovery as the cause of the initial improvement.

Observed signal was approximately -80 to -82 dBm. Weak signal and app contention
are hypotheses, not established causes. A physical power-cycle/closer-range
comparison remains outstanding. Automated tests do not certify radio stability,
physical notification behavior for every control, or all firmware revisions.

## References

Reference code was inspected, not copied into the new model implementation:

- [Homebridge H7123 handler at 998abd02](https://github.com/homebridge-plugins/homebridge-govee/blob/998abd02d1cf7b7fd923780275f445ab1f57f79e/lib/device/purifier-H7123.js)
- [Homebridge H7126 handler used for H712C at 998abd02](https://github.com/homebridge-plugins/homebridge-govee/blob/998abd02d1cf7b7fd923780275f445ab1f57f79e/lib/device/purifier-H7126.js)

The vectors in `tests/test_added_models.py` and the profile JSON make these
application-level observations reproducible without publishing private captures.
