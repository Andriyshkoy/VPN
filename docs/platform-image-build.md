# Build-only platform images

`publish-platform.yml` is a manual image-publishing workflow. It accepts only
the protected `main` ref and confirmation `BUILD_PLATFORM_IMAGES`, verifies
that the exact source SHA is still main, and requires successful push CI for
that SHA. It has no SSH credentials, deployment steps or runtime flag changes.
The historical release/promotion workflows have separate activation behaviour
and must not be substituted for this build-only workflow during a paused rollout.

The build uses the existing Docker Hub `REGISTRY_USERNAME` and
`REGISTRY_PASSWORD` repository secrets and the same six images as the established
release pipeline: admin, bot, billing, migrations, admin frontend and nginx.
All images have the verified source revision label; unique run/attempt tags
avoid overwriting another build's tag. Deployment consumes digests, not tags.

After the workflow succeeds, download artifact `platform-images-<source-sha>`.
It contains:

- `release.env`: `RELEASE_SHA` and exactly six `VPN_*_IMAGE` digest pins.
- `images.json`: source repository/revision, build run/attempt and those pins.

These are image inputs, not a production environment file. They contain no
deployment paths, bearer credentials, billing/payment settings or activation
switches. The private `vpn-infra` release role validates the exact source SHA and
pins, combines them with the existing protected environment, backs up the data,
applies compatible migrations and deploys while preserving the chosen runtime
gates. It separately checks running image identities and post-deploy health.

The four Python image targets share `core/`, so the admin, bot, worker/scheduler
and migrations should move together even when bot menus are unchanged. Worker
and scheduler both use the billing image. The frontend and nginx are also built
from the same release revision to keep the six-component manifest coherent.
