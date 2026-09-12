Audience: whoever copies this overlay into their own deck.

This folder is a Slidev deck. Copy `custom-nav-controls.vue`, `style.css`, and
`components/CoPresenter.vue` next to your `slides.md`. The presenter toolbar
uses the signed-in Presentator origin's relative `/copresenter` routes.
Presentator checks the existing session before readiness, answers, or microphone
capture and keeps the hearing lease open while either local or browser
recognition is active.
How to run the private service is
[`copresenter/README.md`](../../copresenter/README.md).
