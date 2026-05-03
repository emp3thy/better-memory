(function () {
  function initBrutTabs(root) {
    if (root.dataset.brutTabsInit === '1') return;
    root.dataset.brutTabsInit = '1';
    const buttons = root.querySelectorAll('.brut-tab');
    const section = root.closest('.brut-section') || document;
    const panes = section.querySelectorAll('.brut-tab-pane');

    buttons.forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const os = btn.dataset.os;
        buttons.forEach((b) => b.classList.toggle('brut-tab-active', b === btn));
        panes.forEach((p) => p.classList.toggle('brut-tab-pane-active', p.dataset.osPane === os));
      });
    });
  }

  function init() {
    document.querySelectorAll('[data-brut-tabs]').forEach(initBrutTabs);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Material's instant-load nav swaps the page without a full reload.
  // Re-init on each navigation event.
  if (typeof document$ !== 'undefined' && document$.subscribe) {
    document$.subscribe(init);
  }
})();
