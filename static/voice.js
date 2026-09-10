/* Голосовой канал + демонстрация экрана. WebRTC-mesh, сигналинг через Socket.IO.
 *
 * Ключевой приём: у каждого соединения СРАЗУ заводятся два трансивера —
 * audio (с микрофоном) и video (пустой, sendrecv). Демонстрация экрана потом
 * делается через replaceTrack на уже согласованном video-sender'е, поэтому
 * renegotiation не нужна вообще: ни повторных offer/answer, ни glare.
 *
 * Требует глобального ROOM_ID (задаётся в room.html).
 */
(function () {
    'use strict';

    var joinBtn = document.getElementById('voice-btn');
    var screenBtn = document.getElementById('screen-btn');
    var micBtn = document.getElementById('mic-btn');
    var panel = document.getElementById('voice-panel');
    var tilesEl = document.getElementById('voice-tiles');
    var statusEl = document.getElementById('voice-status');
    if (!joinBtn) return;

    var SCREEN_BITRATE = 8000000;   // 8 Мбит/с — читаемый текст в 1080p
    var ICE_DEFAULT = [{ urls: 'stun:stun.l.google.com:19302' }];

    var sock = null;
    var iceServers = ICE_DEFAULT;
    var localStream = null;         // микрофон
    var screenTrack = null;         // текущий трек демонстрации
    var peers = new Map();          // sid -> {pc, videoSender, name, tile, video, audio}

    function setStatus(text) { statusEl.textContent = text; }

    function refreshStatus() {
        if (!sock) { setStatus('не в канале'); return; }
        var names = [];
        peers.forEach(function (p) { names.push(p.name); });
        setStatus(names.length ? 'в канале: вы, ' + names.join(', ')
                               : 'в канале: вы (одни)');
    }

    // ── UI-плитка одного участника ────────────────────────────────────────
    function makeTile(name) {
        var tile = document.createElement('div');
        tile.className = 'voice-tile';

        var video = document.createElement('video');
        video.autoplay = true;
        video.playsInline = true;
        video.hidden = true;

        var audio = document.createElement('audio');
        audio.autoplay = true;

        var label = document.createElement('span');
        label.className = 'voice-tile-name';
        label.textContent = name;

        tile.append(video, audio, label);
        tilesEl.appendChild(tile);
        return { tile: tile, video: video, audio: audio };
    }

    function attachTrack(sid, track, stream) {
        var p = peers.get(sid);
        if (!p) return;
        if (track.kind === 'audio') { p.audio.srcObject = stream; return; }

        p.video.srcObject = stream;
        // Пустой video-трансивер приходит muted; показываем плитку только
        // когда собеседник реально включил демонстрацию.
        var sync = function () {
            p.video.hidden = track.muted;
            p.tile.classList.toggle('has-video', !track.muted);
        };
        track.addEventListener('mute', sync);
        track.addEventListener('unmute', sync);
        sync();
    }

    // ── Peer connection ───────────────────────────────────────────────────
    function ensurePeer(sid, name) {
        var existing = peers.get(sid);
        if (existing) return existing;

        var pc = new RTCPeerConnection({ iceServers: iceServers });
        pc.addTrack(localStream.getAudioTracks()[0], localStream);
        var videoSender = pc.addTransceiver('video', { direction: 'sendrecv' }).sender;

        pc.onicecandidate = function (e) {
            if (e.candidate) send(sid, { candidate: e.candidate });
        };
        pc.ontrack = function (e) { attachTrack(sid, e.track, e.streams[0]); };
        pc.onconnectionstatechange = function () {
            if (pc.connectionState === 'failed') closePeer(sid);
        };

        var ui = makeTile(name);
        var entry = {
            pc: pc, videoSender: videoSender, name: name,
            tile: ui.tile, video: ui.video, audio: ui.audio
        };
        peers.set(sid, entry);
        if (screenTrack) applyScreenTo(entry, screenTrack);
        refreshStatus();
        return entry;
    }

    function closePeer(sid) {
        var p = peers.get(sid);
        if (!p) return;
        try { p.pc.close(); } catch (e) { /* уже закрыт */ }
        p.tile.remove();
        peers.delete(sid);
        refreshStatus();
    }

    function send(sid, data) {
        if (sock) sock.emit('voice_signal', { to: sid, data: data });
    }

    async function offerTo(sid, name) {
        var p = ensurePeer(sid, name);
        var offer = await p.pc.createOffer();
        await p.pc.setLocalDescription(offer);
        send(sid, { sdp: p.pc.localDescription });
    }

    async function onSignal(from, data) {
        if (!data) return;

        if (data.candidate) {
            var known = peers.get(from);
            if (known) {
                try { await known.pc.addIceCandidate(data.candidate); }
                catch (e) { /* кандидат пришёл раньше описания — ICE переживёт */ }
            }
            return;
        }
        if (!data.sdp) return;

        if (data.sdp.type === 'offer') {
            var p = ensurePeer(from, data.name || 'участник');
            await p.pc.setRemoteDescription(data.sdp);
            var answer = await p.pc.createAnswer();
            await p.pc.setLocalDescription(answer);
            send(from, { sdp: p.pc.localDescription });
        } else if (data.sdp.type === 'answer') {
            var q = peers.get(from);
            if (q) await q.pc.setRemoteDescription(data.sdp);
        }
    }

    // ── Вход / выход ──────────────────────────────────────────────────────
    async function join() {
        localStream = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
            video: false
        });

        // ponytail: отдельное Socket.IO-соединение, чтобы не трогать чатовый
        // сокет в room.html. Если это станет проблемой — переиспользовать его.
        sock = io({ transports: ['websocket'] });
        sock.on('connect', function () { sock.emit('voice_join', { room_id: ROOM_ID }); });
        sock.on('voice_peers', function (d) {
            iceServers = d.ice || ICE_DEFAULT;
            // Уже сидящие в канале сами пришлют offer — нам остаётся ответить.
            (d.peers || []).forEach(function (p) { ensurePeer(p.sid, p.name); });
            refreshStatus();
        });
        sock.on('voice_peer_joined', function (p) { offerTo(p.sid, p.name); });
        sock.on('voice_signal', function (d) { onSignal(d.from, d.data); });
        sock.on('voice_peer_left', function (d) { closePeer(d.sid); });

        panel.hidden = false;
        joinBtn.textContent = 'Выйти';
        screenBtn.disabled = false;
        micBtn.disabled = false;
        refreshStatus();
    }

    function leave() {
        stopScreen();
        if (sock) { sock.emit('voice_leave'); sock.disconnect(); sock = null; }
        Array.from(peers.keys()).forEach(closePeer);
        if (localStream) {
            localStream.getTracks().forEach(function (t) { t.stop(); });
            localStream = null;
        }
        panel.hidden = true;
        joinBtn.textContent = 'Голос';
        screenBtn.disabled = true;
        screenBtn.textContent = 'Экран';
        micBtn.disabled = true;
        refreshStatus();
    }

    // ── Демонстрация экрана ───────────────────────────────────────────────
    async function applyScreenTo(peer, track) {
        await peer.videoSender.replaceTrack(track);
        var params = peer.videoSender.getParameters();
        if (!params.encodings || !params.encodings.length) params.encodings = [{}];
        params.encodings[0].maxBitrate = SCREEN_BITRATE;
        params.degradationPreference = 'maintain-resolution';  // чётче текст, реже кадры
        try { await peer.videoSender.setParameters(params); }
        catch (e) { /* часть браузеров не даёт менять параметры на лету */ }
    }

    async function startScreen() {
        var stream = await navigator.mediaDevices.getDisplayMedia({
            video: { frameRate: { ideal: 30, max: 60 }, width: { ideal: 1920 }, height: { ideal: 1080 } },
            audio: false   // ponytail: звук вкладки требует второго audio-трансивера, добавить если попросят
        });
        screenTrack = stream.getVideoTracks()[0];
        screenTrack.contentHint = 'detail';
        screenTrack.onended = stopScreen;   // кнопка «остановить» в панели браузера
        peers.forEach(function (p) { applyScreenTo(p, screenTrack); });
        screenBtn.textContent = 'Стоп экран';
    }

    function stopScreen() {
        if (!screenTrack) return;
        screenTrack.onended = null;
        screenTrack.stop();
        screenTrack = null;
        peers.forEach(function (p) { p.videoSender.replaceTrack(null); });
        screenBtn.textContent = 'Экран';
    }

    // ── Кнопки ────────────────────────────────────────────────────────────
    joinBtn.addEventListener('click', function () {
        if (sock) { leave(); return; }
        join().catch(function (e) {
            setStatus('микрофон недоступен: ' + e.name);
            leave();
        });
    });

    screenBtn.addEventListener('click', function () {
        if (screenTrack) { stopScreen(); return; }
        startScreen().catch(function () { /* пользователь отменил выбор окна */ });
    });

    micBtn.addEventListener('click', function () {
        if (!localStream) return;
        var track = localStream.getAudioTracks()[0];
        track.enabled = !track.enabled;
        micBtn.textContent = track.enabled ? 'Микрофон' : 'Микрофон выкл';
    });

    window.addEventListener('pagehide', leave);
})();
