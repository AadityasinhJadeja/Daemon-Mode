(function () {
  if (window.location.protocol !== 'file:') return;

  document.querySelectorAll('a[href]').forEach(function (link) {
    var raw = link.getAttribute('href');
    if (!raw || raw.charAt(0) === '#' || /^(https?:|mailto:|tel:)/.test(raw)) return;

    var hashIndex = raw.indexOf('#');
    var path = hashIndex >= 0 ? raw.slice(0, hashIndex) : raw;
    var hash = hashIndex >= 0 ? raw.slice(hashIndex) : '';

    if (path === '/') path = './index.html';
    else if (path.slice(-1) === '/') path += 'index.html';

    link.setAttribute('href', path + hash);
  });
})();
