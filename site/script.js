// Vote handling
async function vote(slug, direction) {
  try {
    const res = await fetch('/api/vote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, direction })
    });
    if (!res.ok) return;
    const data = await res.json();
    updateVoteUI(slug, data);
  } catch (e) {
    console.error('Vote failed:', e);
  }
}

function updateVoteUI(slug, data) {
  const widget = document.querySelector(`[data-slug="${slug}"]`);
  if (!widget) return;

  const score = widget.querySelector('.vote-score');
  const upBtn = widget.querySelector('.vote-up');
  const downBtn = widget.querySelector('.vote-down');

  score.textContent = data.score;
  upBtn.classList.toggle('active-up', data.user_vote === 'up');
  downBtn.classList.toggle('active-down', data.user_vote === 'down');
}

async function loadVotes(slug) {
  try {
    const res = await fetch(`/api/votes/${slug}`);
    if (!res.ok) return;
    const data = await res.json();
    updateVoteUI(slug, data);
  } catch (e) {
    // silent fail
  }
}

// Subscribe handling
async function subscribe(form) {
  const input = form.querySelector('input[type="email"]');
  const msg = form.parentElement.querySelector('.subscribe-msg');
  const email = input.value.trim();

  if (!email) return;

  try {
    const res = await fetch('/api/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email })
    });
    const data = await res.json();
    if (data.ok) {
      msg.textContent = 'subscribed.';
      msg.className = 'subscribe-msg ok';
      input.value = '';
    } else {
      msg.textContent = data.error || 'something went wrong.';
      msg.className = 'subscribe-msg err';
    }
  } catch (e) {
    msg.textContent = 'network error.';
    msg.className = 'subscribe-msg err';
  }
}

// Init: load votes for all widgets on page
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.vote-widget').forEach(w => {
    const slug = w.dataset.slug;
    if (slug) loadVotes(slug);
  });

  document.querySelectorAll('.subscribe-form').forEach(form => {
    form.addEventListener('submit', e => {
      e.preventDefault();
      subscribe(form);
    });
  });
});
