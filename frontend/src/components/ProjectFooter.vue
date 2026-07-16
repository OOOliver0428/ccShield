<template>
  <footer class="project-footer" aria-label="项目署名" data-testid="project-footer">
    <span>ccShield © 2026</span>
    <a
      href="https://github.com/OOOliver0428"
      target="_blank"
      rel="noopener noreferrer"
    >
      OOOliver0428
    </a>
    <span>and contributors</span>
    <span class="separator" aria-hidden="true">·</span>
    <a
      href="https://github.com/OOOliver0428/ccShield/blob/main/LICENSE"
      target="_blank"
      rel="noopener noreferrer"
    >
      MIT License
    </a>
    <span class="separator" aria-hidden="true">·</span>
    <a
      href="https://github.com/OOOliver0428/ccShield"
      target="_blank"
      rel="noopener noreferrer"
    >
      GitHub
    </a>
  </footer>
</template>

<style scoped>
.project-footer {
  display: flex;
  min-height: 28px;
  padding: 7px 10px 1px;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 4px;
  color: var(--cc-text-muted);
  font-size: 10px;
  line-height: 1.5;
  text-align: center;
}

.project-footer a {
  border-radius: 3px;
  color: var(--cc-text-secondary);
  text-decoration: none;
  transition: color 160ms ease;
}

.project-footer a:hover {
  color: var(--cc-primary-hover);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.separator {
  margin-inline: 2px;
  color: var(--cc-border-strong);
}
</style>
