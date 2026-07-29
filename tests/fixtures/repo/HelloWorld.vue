<template>
  <div>{{ message }}</div>
</template>

<script setup lang="ts">
import { ref } from "vue";

const message = ref("hello");

function greet(name: string): string {
  return `Hello, ${name}`;
}
</script>
