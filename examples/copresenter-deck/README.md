Audience: whoever copies this overlay into their own deck.

This folder is a Slidev deck. Copy `global-bottom.vue` and
`components/CoPresenter.vue` next to your `slides.md`. The overlay talks to the
co-presenter service at `http://127.0.0.1:3040`; a deck whose service is
somewhere else sets the address in its own `global-bottom.vue`:

```vue
<script setup>
import CoPresenter from './components/CoPresenter.vue'

if (typeof window !== 'undefined') {
  window.COPRESENTER_URL = 'https://copresenter.example.com'
}
</script>
```

The deck is the only place that names the address: a talk URL cannot redirect
the overlay, so a shared link cannot send the microphone elsewhere. The service
answers only the origin its `COPRESENTER_ALLOWED_ORIGIN` names — every route and
the hearing socket — so that value is the origin the deck is served from. How to
run the service is
[`copresenter/README.md`](../../copresenter/README.md).
