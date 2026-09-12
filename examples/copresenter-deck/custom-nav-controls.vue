<script setup>
import { useNav } from '@slidev/client'

import CoPresenter from './components/CoPresenter.vue'

const { isPresenter } = useNav()
</script>

<template>
  <template v-if="isPresenter">
    <a
      class="presenter-home slidev-icon-btn"
      href="/"
      aria-label="Home"
      title="Home"
    >
      <div
        class="i-carbon:home"
        aria-hidden="true"
      />
      <span>Home</span>
    </a>
    <CoPresenter />
  </template>
</template>
