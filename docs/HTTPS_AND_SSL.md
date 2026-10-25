# HTTPS, SSL/TLS, and certificate status

## Current status

The repository has a Render Blueprint (render.yaml) but that file alone does not prove a service has been deployed, that a domain points to it, or that a certificate is active. No verified public deployment URL is currently recorded in this repository's release evidence. Do not display an “SSL active” claim until the deployed hostname is checked.

TLS is the current protocol; “SSL certificate” is the common name for the certificate. Use HTTPS and a valid TLS certificate for all production traffic.

## Render deployment

Render documents that web services receive a managed onrender.com hostname and fully managed TLS certificates, with automatic HTTP-to-HTTPS redirects. A custom domain requires adding the domain to the service, setting the DNS records requested by Render, and verifying it in the Render Dashboard. Render then issues and renews the certificate.

- [Render managed TLS certificates](https://render.com/docs/tls)
- [Render custom domains](https://render.com/docs/custom-domains)
- [Render web services](https://render.com/docs/web-services)

This repository has not been confirmed as deployed to a Render service. Do not create or claim a certificate until a real service/domain is connected.

## Required production configuration

Configure the deployment environment (never commit secrets):

- APP_ENV=production
- SECRET_KEY: a stable, random secret
- ADMIN_USERNAME and ADMIN_PASSWORD: unique credentials
- REQUIRE_API_AUTH=1
- Keep secure cookies enabled; production security configuration forces SESSION_COOKIE_SECURE=True.

Use the hosting platform's HTTPS termination and redirect features. Do not expose the Flask development server publicly. Use Gunicorn as configured in Procfile and render.yaml. Before production, verify forwarded-proxy assumptions, trusted hosts, persistence, backups, database migrations, and rollback procedures.

## Check a deployed certificate

The repository includes a standard-library-only checker that validates the TLS certificate against the operating system trust store, checks hostname verification through Python's TLS client, rejects non-HTTPS URLs, and refuses non-public DNS destinations.

After deployment, run:

    python tools/check_tls.py https://YOUR-REAL-PUBLIC-HOST/health

Optional flags:

    python tools/check_tls.py https://YOUR-REAL-PUBLIC-HOST/health --timeout 10 --warn-days 30

A successful result reports the negotiated TLS version, cipher, certificate validity, and days remaining. It does not test the full application security posture, authentication, or every route.

## GitHub TLS-check badge

The workflow TLS Certificate Check is manually triggered and requires the actual public HTTPS URL. Its badge indicates the result of the latest run; before the first successful run it must not be interpreted as evidence that a certificate is active. Never put credentials or private URLs in the workflow input.

## Acceptance criteria

- [ ] A real deployment URL is recorded and responds on HTTPS.
- [ ] Certificate chain and hostname validation pass from a clean runner.
- [ ] HTTP redirects to HTTPS at the hosting edge.
- [ ] Production session cookies are Secure, HttpOnly, and SameSite=Lax.
- [ ] Certificate expiry monitoring is run after deployment and repeated on a schedule.
- [ ] Authentication, authorization, tenant isolation, backups, and restore are independently verified.
