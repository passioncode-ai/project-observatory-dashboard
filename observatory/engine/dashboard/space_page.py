"""Space controls reuse the dashboard's existing tokens; behavior is in space.js."""
from i18n import Translator


def space_html(payload, t=None):
    t = t or Translator()
    return ('<section id="space-manager" class="space-manager">'
            + '<div id="space-status" role="status" aria-live="polite"></div>'
            + '<section class="card panel">'
            + t.mark('Cache maintenance', tag='h2', attrs=' class="machine-h"')
            + t.mark('Only approved cache operations run here. Working files, sessions and containers are preserved.', tag='p')
            + '<div class="space-actions">'
            + t.mark('Preview cleanup', tag='button', attrs=' id="space-preview" class="chip-btn" type="button"')
            + t.mark('Measure caches', tag='button', attrs=' id="space-scan" class="chip-btn" type="button"')
            + '</div><label class="space-policy"><input type="checkbox" id="space-auto"> '
            + t.mark('Automatically clean approved caches when disk space is low')
            + '</label><p id="space-threshold" class="machine-hint"></p></section>'
            + '<section id="space-plan" class="card panel" hidden tabindex="-1"></section>'
            + '<section class="card panel">' + t.mark('Cache register', tag='h2', attrs=' class="machine-h"')
            + t.mark('Sizes show occupied space, not guaranteed recovery. Protected caches stay in place.', tag='p')
            + '<div id="space-caches"></div></section>'
            + '<section class="card panel">' + t.mark('Cleanup history', tag='h2', attrs=' class="machine-h"')
            + '<div id="space-history"></div></section>'
            + '<section class="card panel">' + t.mark('Space notifications', tag='h2', attrs=' class="machine-h"')
            + '<div id="space-notifications"></div></section></section>')
