# Custom Auto sampling and handoff

Status: accepted and implemented in the working tree (not a release claim).

Custom Auto uses source option 3: authoritative event-driven PM2.5 observations
plus bounded one-shot `aa 19` requests at activation, confirmation, or matured
downshift boundaries. Boundary events may request a sample but never issue a fan
command; only a fresh authoritative observation can do that. There is no fixed
PM2.5 polling cadence, and the existing `aa 01` health poll remains unchanged.
Use of the `aa 19` response as authoritative PM2.5 is a maintainer-approved
implementation assumption, not newly established wire evidence.

After a level is confirmed, positive upshifts require two distinct authoritative
revisions separated by the configured confirmation window; zero confirmation
permits the first reading. Activation with unknown ownership uses the first
fresh valid reading to choose an initial target. Downshifts start from one sample
and require a fresh still-qualifying sample at or after maturity.

Turning Custom Auto off, or disabling the option, requests hardware Auto only
when the purifier is already powered on and the application channel is usable.
When it is off, the integration clears Custom Auto intent without powering the
purifier on. Unknown power or temporary unavailability leaves the truthful OFF
state with handoff not attempted; command failure is retained as a failed
handoff.
