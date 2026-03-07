// Service Worker для уведомлений МЫС Web

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            // фокусируем уже открытую вкладку
            for (var i = 0; i < clientList.length; i++) {
                if ('focus' in clientList[i]) {
                    return clientList[i].focus();
                }
            }
            // или открываем новую
            if (clients.openWindow) {
                return clients.openWindow('/');
            }
        })
    );
});
