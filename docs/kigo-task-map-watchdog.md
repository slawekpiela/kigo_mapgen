# Kigo Task Map Watchdog

Anton runs the Kigo task map HTTP service as Docker container
`kigo-task-map-service` on port `8787`.

The watchdog checks:

```bash
curl -fsS --max-time 4 http://127.0.0.1:8787/health
```

Expected response:

```text
ok
```

If the health check fails or returns anything other than `ok`, the watchdog
restarts only this container:

```bash
docker restart kigo-task-map-service
```

It then sends an alert to `kigo@kigoconcept.pl` with the failure, restart
result, post-restart health result, and container status.

## Anton Install

Install/update the script:

```bash
install -m 0755 tools/kigo-task-map-watchdog /home/slawek/bin/kigo-task-map-watchdog
```

Install the cron entry for user `slawek`:

```cron
*/5 * * * * /home/slawek/bin/kigo-task-map-watchdog
```

Logs are written only when the watchdog skips due to an active lock or performs
a restart:

```text
/home/slawek/kigo-task-map-service/watchdog.log
```

The script uses `/usr/sbin/sendmail`, so Anton's local MTA must stay configured
for outbound mail.
