// Re-theme Material's bundled mermaid with the brutalist palette.
// Subscribes to Material's document$ observable so it survives instant-nav.

(function () {
  const themeConfig = {
    startOnLoad: false,
    theme: 'base',
    themeVariables: {
      fontFamily: '"JetBrains Mono", monospace',
      fontSize: '14px',
      primaryColor: '#f5f3ee',
      primaryTextColor: '#0d0d0d',
      primaryBorderColor: '#0d0d0d',
      lineColor: '#0d0d0d',
      secondaryColor: '#ff8c2b',
      tertiaryColor: '#ebe8e1',
      background: '#f5f3ee',
      edgeLabelBackground: '#f5f3ee',
      textColor: '#0d0d0d',
      // State-diagram specific
      stateBkg: '#f5f3ee',
      stateBorder: '#0d0d0d',
      labelColor: '#0d0d0d',
      altBackground: '#ebe8e1',
      cScale0: '#ff8c2b',
      cScale1: '#ebe8e1',
      cScale2: '#f5f3ee'
    }
  };

  function applyTheme() {
    if (!window.mermaid) return;
    try {
      window.mermaid.initialize(themeConfig);
      // Re-render any already-rendered diagrams
      const blocks = document.querySelectorAll('.mermaid');
      if (blocks.length && typeof window.mermaid.run === 'function') {
        // Reset processed flag so mermaid will re-render
        blocks.forEach((b) => b.removeAttribute('data-processed'));
        window.mermaid.run({ nodes: blocks });
      }
    } catch (e) {
      console.warn('Brutalist mermaid theme: re-init failed', e);
    }
  }

  // Material exposes document$ (rxjs-style) for page swaps.
  if (typeof document$ !== 'undefined' && document$.subscribe) {
    document$.subscribe(() => setTimeout(applyTheme, 50));
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(applyTheme, 50));
  } else {
    setTimeout(applyTheme, 50);
  }
})();
