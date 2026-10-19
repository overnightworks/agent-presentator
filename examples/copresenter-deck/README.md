Audience: whoever copies this overlay into their own deck.

This folder is a Slidev deck. Copy `custom-nav-controls.vue`, `style.css`,
`components/CoPresenter.vue`, and `public/pcm-capture-processor.js` next to
your `slides.md` — the last one has to land in your own deck's `public/`
folder, because `CoPresenter.vue` loads it as a same-origin AudioWorklet
module at a URL Vite only resolves there. The presenter toolbar uses the
signed-in Presentator origin's relative `/copresenter` routes.
Presentator checks the existing session before readiness, answers, or microphone
capture and keeps the hearing lease open only while local hearing is active.
How to run the private service is
[`copresenter/README.md`](../../copresenter/README.md).
