# Base de Connaissance — Laser Quiz (Quiz Arena Live + Photobooth)

> Documentation technique et fonctionnelle complète de l'application **Laser Quiz** (`FastAPI` + WebSockets + SPA TV/Mobile + intégration Photobooth Simple Booth).  
> Branche Git active : `merge-photobooth` (dépôt `JoSurvie/quiz-arena-live`, `main` antérieur au commit `bacdf18`).

---

## 1. Vue d'ensemble & Architecture des fichiers

**Laser Quiz** est une application de quiz multijoueur en temps réel conçue pour fonctionner simultanément sur un **écran TV en salle** (affichage d'attente, QR code, questions, animations de gamification, bandeau photobooth) et sur **smartphones joueurs** (connexion sans compte via QR code, vote de catégorie, buzzer tactile, bilan et partage social).

| Fichier | Rôle principal | Détails clés |
| --- | --- | --- |
| [server.py](server.py) | Serveur FastAPI, boucle d'horloge, WebSockets, proxy Simple Booth & push Google Sheets | Titre `FastAPI(title="Laser Quiz")`, gestion des salles (`Room`), joueurs (`Player`), tirage non répétitif, gamification, nettoyage AFK/déconnexion, cache photobooth et suivi KPI fin de partie |
| [static/index.html](static/index.html) | Interface SPA unique (TV, Mobile Joueur & Régie Staff) | Routage automatique par URL (`?tv_auth=...` -> `initStaffAuth()`, `?room=XXXXX` -> `initPlayer()`, sinon `initTV()`). Stack CDN : Tailwind CSS (`https://cdn.tailwindcss.com`), QRCode.js (`1.0.0`), `canvas-confetti` (`1.9.3`) |
| [questions.json](questions.json) | Banque de questions structurée | **300 questions uniques** : 5 catégories de base × 2 niveaux (`standard` et `debutant`) × 30 questions |
| [google_sheets_webhook.gs](google_sheets_webhook.gs) | Script Google Apps Script (Webhook Drive) | À coller dans *Extensions → Apps Script* du Google Sheet cible (`1ZJ-sDjjC74DrQmhNJDXOargA7OH-4HJJ2ecenpQX0sI`) pour recevoir et mettre à jour les 6 colonnes KPI automatiquement |
| [requirements.txt](requirements.txt) | Dépendances Python | `fastapi==0.115.0`, `uvicorn[standard]==0.30.6`, `websockets==13.1` |
| [.gitignore](.gitignore) | Exclusions Git | `__pycache__/`, `*.py[cod]`, `.venv/`, `venv/`, `.env`, `.DS_Store` |

### Routage d'interface & Branding
- **Détection du mode** dans [static/index.html](static/index.html) : `?tv_auth=CODE` lance `initStaffAuth()` (connexion/régie staff sur cellulaire), `?room=XXXXX` lance `initPlayer()` (interface joueur mobile), sinon `initTV()` (écran TV + régie).
- **Codes de salle** : `CODE_LENGTH = 5` caractères tirés depuis `CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"` (exclusion des caractères ambigus `I`, `O`, `0`, `1`).
- **Durée de vie d'une salle vide** : `ROOM_TTL = 30 * 60` secondes (1800 s / 30 min, suivi via `Room.empty_since`).
- **Logo Laser Game Evolution** : PNG transparent officiel affiché sans cadre blanc (`https://lasergame-evolution.ca/wp-content/uploads/2025/01/CANADA-vector-logo-color-bl-black-copie-01.png`).
- **QR codes sur TV** : grand QR code affiché au centre en phase `LOBBY`, relayé par un mini QR code permanent dans les autres phases pour permettre de rejoindre en cours de route.

---

## 2. Machine à états & Boucle d'horloge (`game_clock_loop`)

L'état d'une partie est piloté côté serveur par `Room.game_clock_loop()`, `Room.on_timer_expired()` et `Room.advance()`. À chaque changement ou tick, `Room.broadcast()` diffuse l'état adapté à tous les écrans TV (`tv_state()`) et à chaque joueur (`player_state()`).

### Phases et durées officielles

| Phase | Constante | Durée par défaut | Comportement |
| --- | --- | --- | --- |
| `LOBBY` | `LOBBY_DURATION` | **240 s** (4 min) | Attente des joueurs, vote de catégorie, activation option Débutant, affichage du bandeau Photobooth |
| `QUESTION` | `QUESTION_DURATION` | **20 s** | Affichage de la question et des 4 choix mélangés ; enregistrement du `answer_remaining` ; passage anticipé possible si tous les joueurs actifs ont répondu |
| `REVEAL` | `REVEAL_DURATION` | **10 s** | Révélation de la bonne réponse, distribution des choix (`counts`, `choice_players`), calcul des scores, déclenchement des événements de gamification (`round_events`) |
| `INTER_PODIUM` | `INTER_PODIUM_DURATION` | **10 s** | Classement intermédiaire affiché toutes les `CHECKPOINT_INTERVAL = 5` questions (après Q5 et Q10 sur `QUESTIONS_PER_GAME = 15`) |
| `PODIUM` | `PODIUM_DURATION` | **60 s** | Podium final, badges obtenus, carte de score partageable sur mobile, possibilité de voter ou basculer la vitesse/régie |

### Contrôle de vitesse & Régie (`#regie`)
- **Multiplicateur de vitesse (`Room.speed`)** : `1` (normal) ou `5` (accéléré ×5 pour tests/démonstrations), basculé via la commande régie `TOGGLE_SPEED`.
- **Commandes régie disponibles sur l'écran TV (`⚙️`)** :
  - Passer immédiatement à l'étape suivante (`NEXT`)
  - Basculer la vitesse `1x / 5x` (`TOGGLE_SPEED`)
  - Activer/désactiver le vote des joueurs (`TOGGLE_VOTE`)
  - Sélectionner / lancer une catégorie manuellement (`SELECT_CATEGORY` ou `START_NEXT_CATEGORY`)
  - Afficher un aperçu mobile de test
  - Masquer/afficher et configurer le Photobooth (`#btn-pb-toggle` + cookies locaux)
  - Expulser manuellement un joueur (`KICK_PLAYER` via bouton `✕`)
  - **Défilement manuel sur Smart TV (`#regie-body`)** : boutons `⬆️ / ⬇️` en haut (`#regie-up-top`, `#regie-down-top`) et barre fixe `⬆️ Monter / ⬇️ Descendre` en bas (`#regie-up-bot`, `#regie-down-bot`), plus navigation clavier/télécommande (`ArrowUp`, `ArrowDown`, `PageUp`, `PageDown`) sans conteneurs imbriqués

---

## 3. Banque de 300 questions, `Mix Aléatoire` & Tirage non répétitif

### Structure de `questions.json`
La banque contient **300 questions** réparties équitablement entre **5 catégories de base** (`BASE_CATEGORIES`) et **2 niveaux de difficulté** :

| Catégorie | Niveau `standard` | Niveau `debutant` | Total |
| --- | :---: | :---: | :---: |
| `Histoire & Géographie` | 30 | 30 | 60 |
| `Sciences & Nature` | 30 | 30 | 60 |
| `Cinéma & Pop Culture` | 30 | 30 | 60 |
| `Arts, Musique & Littérature` | 30 | 30 | 60 |
| `Sport & Gastronomie` | 30 | 30 | 60 |
| **Total** | **150** | **150** | **300** |

Une 6ᵉ catégorie virtuelle est exposée dans `CATEGORIES = BASE_CATEGORIES + [MIX_CATEGORY]` : **`Mix Aléatoire`** (`MIX_CATEGORY = "Mix Aléatoire"`), qui pioche dans l'ensemble des 5 catégories pour le niveau actif.

### Mécanique de tirage (`_draw_questions`) & anti-répétition
1. **Indexation** : au démarrage du serveur, les questions sont indexées dans `QUESTIONS_BY_CATEGORY` et `QUESTIONS_BY_CAT_DIFF`.
2. **Aléatoire cryptographique** : le tirage et les mélanges utilisent `SYS_RNG = random.SystemRandom()` (source système `os.urandom`), évitant les séquences prévisibles ou identiques d'une partie à l'autre.
3. **Historique par salle (`Room.seen_questions`)** : les questions déjà jouées dans une salle (par couple catégorie/difficulté) sont mémorisées dans `seen_questions` et exclues des tirages suivants tant que le réservoir n'est pas épuisé. Une fois toutes les questions vues, le pool pour cette combinaison se réinitialise proprement.
4. **Mélange des 4 réponses** : chaque question tirée voit ses 4 choix (`▲ ◆ ● ■`) remélangés et son index de bonne réponse recalculé avant diffusion, empêchant de mémoriser l'emplacement d'une bonne réponse.
5. **Parties de 15 questions** : chaque manche tire `QUESTIONS_PER_GAME = 15` questions.

---

## 4. Vote de catégorie, Verrouillage & Mode Débutant

### Vote & Sélection (`voting_enabled`, `apply_votes`)
- Quand `voting_enabled` est actif, les joueurs connectés peuvent voter pour une catégorie depuis leur smartphone (`VOTE` ou dès le `JOIN`).
- `_leading_voted_category()` détermine la catégorie en tête parmi `available_categories()` (à égalité ou absence de vote, fallback géré par la salle).
- Les boutons de sélection manuelle de catégories ont été déplacés dans le panneau **Régie (`#regie-cats`)** afin de laisser le Lobby TV épuré (QR code + joueurs + Photobooth).

### Verrouillage & Déverrouillage automatique (`refresh_category_locks`)
- Une catégorie jouée est ajoutée à `Room.played_categories` et verrouillée pour les manches suivantes afin de varier les thèmes.
- Lors du retour au lobby / préparation de la manche suivante, `refresh_category_locks()` compare la liste des joueurs actuels aux joueurs de la partie précédente (`Room.last_game_players`) :
  - **Si $\ge 50\%$ des joueurs précédents ne participent plus**, `played_categories` est automatiquement réinitialisé afin qu'un nouveau groupe retrouve toutes les catégories disponibles.

### Passage automatique en Mode Débutant (`beginner_stats`)
- Chaque joueur dispose d'une case **`🌱 Débutant`** modifiable à l'inscription (`JOIN`) et en direct en `LOBBY` ou `PODIUM` (`VOTE` avec `beginner`).
- `Room.beginner_stats()` évalue le ratio de joueurs connectés ayant coché l'option :
  - Si **strictement plus de 50 %** des joueurs connectés sont en mode Débutant (`debutant > total / 2`), `Room.active_difficulty` bascule automatiquement sur `"debutant"` pour le tirage suivant ; sinon il reste sur `"standard"`.

---

## 5. Calcul des scores, Gamification (10 événements) & Partage social

### Formule de score
Pour une bonne réponse enregistrée avec `answer_remaining` secondes restantes sur `QUESTION_DURATION = 20` s :

```python
pts = max(500, min(1000, round(500 + 500 * (p.answer_remaining / 20))))
```

- **Plancher** : `MIN_POINTS = 500` pts (réponse juste à la dernière fraction de seconde).
- **Plafond** : `MAX_POINTS = 1000` pts (réponse quasi instantanée).
- **Réponse fausse ou absente** : `0` pt.

### Les 10 événements de gamification (`round_events` & Badges)
Calculés lors de `Room.reveal()` et ordonnés par priorité pour l'affichage animé à l'écran TV (`_make_event`), puis enregistrés dans `Player.badges` via `award_badge()` :

| Priorité | Code événement (`kind`) | Libellé affiché | Condition de déclenchement |
| :---: | --- | --- | --- |
| 1 | `STREAK_15` | `👑 SANS-FAUTE ABSOLU !` | 15 bonnes réponses consécutives (`correct_streak == 15`) |
| 2 | `STREAK_10` | `⚡ SÉRIE LÉGENDAIRE !` | 10 bonnes réponses consécutives (`correct_streak == 10`) |
| 3 | `FASTEST_RECORD` | `🚀 RECORD DE VITESSE !` | Réponse juste la plus rapide de toute la partie (`Room.fastest_record` battu) |
| 4 | `STREAK_5` | `🔥 SÉRIE EN FEU !` | 5 bonnes réponses consécutives (`correct_streak == 5`) |
| 5 | `LONE_WOLF` | `💀 SEUL CONTRE TOUS !` | Un seul joueur dans toute la salle a trouvé la bonne réponse sur la question |
| 6 | `WRONG_5` | `🧊 COUP DE FROID !` | 5 mauvaises réponses consécutives (déclenché aux multiples de 5 sur `wrong_streak`) |
| 7 | `NEW_LEADER` | `🥇 NOUVEAU LEADER !` | Changement de 1ʳᵉ place au classement général |
| 8 | `COMEBACK` | `🦅 REMONTADA !` | Progression de +3 places ou plus au classement sur la manche |
| 9 | `BUZZER_BEATER` | `🍀 SAUVÉ PAR LE GONG !` | Bonne réponse envoyée à `answer_remaining <= 2.0` secondes de la fin |
| 10 | `FIRST_BLOOD` | `🎯 DÉGAINÉ EN PREMIER !` | Premier joueur à avoir soumis une bonne réponse sur la question courante |

### Partage social en fin de partie (Mobile — phase `PODIUM`)
- L'interface mobile génère dynamiquement une **carte image PNG `1080×1350`** sur `<canvas>` HTML5 contenant le **logo Laser Game Evolution** tout en haut (chargé sans erreur CORS via le proxy `/api/logo`), le branding Laser Quiz, l'avatar, le pseudo, le rang final, le score total, la catégorie jouée et les badges gagnés.
- Boutons de partage disponibles :
  - **Partage natif (`navigator.share`)** avec fichier PNG si supporté par le navigateur mobile
  - Raccourcis directs **X (Twitter)**, **Facebook**, **WhatsApp**
  - **Copie du résumé** dans le presse-papiers

---

## 6. Prévention des joueurs fantômes & Nettoyage AFK (5 protections)

Pour éviter qu'un joueur qui ferme son navigateur ou pose son téléphone ne bloque la salle ou le passage automatique des questions :

1. **Grâce de déconnexion différenciée (`prune_inactive_players`)** :
   - En `LOBBY` ou `PODIUM` : suppression automatique au bout de **`DISCONNECT_GRACE_LOBBY = 5.0` secondes** après perte de connexion WebSocket (`disconnected_at`).
   - En cours de partie (`QUESTION` / `REVEAL` / `INTER_PODIUM`) : grâce étendue à **`DISCONNECT_GRACE_GAME = 30.0` secondes** pour tolérer un rafraîchissement réseau mobile.
2. **Exclusion automatique AFK après 5 questions manquées (`AFK_MISSED_LIMIT = 5`)** :
   - Si un joueur ne répond pas à la question, son compteur `missed_streak` augmente (et son compteur de réponses justes est remis à zéro).
   - Dès `missed_streak >= 5`, le joueur est retiré de la salle et reçoit l'événement WebSocket `{"type": "kicked", "reason": ...}` (mis en file via `pending_kicks`).
3. **Retour automatique au Lobby si la salle se vide (`check_empty_active_game`)** :
   - Si une partie avait des participants (`game_had_players = True`) et que tous les joueurs quittent ou sont exclus pendant une phase active, la salle appelle automatiquement `back_to_lobby()` sans attendre la fin des 15 questions.
4. **Bouton `🚪 Quitter la partie` en bas du mobile (`#pl-footer` / `#btn-leave`)** :
   - Positionné tout en bas de l'écran joueur pour éviter les appuis accidentels tout en restant accessible.
   - Envoie le message WebSocket `LEAVE` et positionne les verrous côté client `manualJoinRequired` et `kickedReason` afin d'empêcher la reconnexion automatique silencieuse si l'onglet se réveille.
5. **Expulsion manuelle depuis la Régie TV (`KICK_PLAYER`)** :
   - Liste des joueurs en direct dans `#regie-players` avec bouton `✕` envoyant `{"type": "KICK_PLAYER", "player_id": ...}`.

---

## 7. Intégration Photobooth Simple Booth

### Magasins préconfigurés (`DEFAULT_STORES` dans `server.py` & régie TV)
Les 7 succursales préconfigurées avec leurs URL Simple Booth et codes de galerie extraits sont :

| Magasin | Code galerie (`galleryCode`) | URL Simple Booth configurée |
| --- | --- | --- |
| `Sainte-Foy` | `3DTLTk68fwVM` | `https://www.simplebooth.com/gallery/3DTLTk68fwVM-lge-fev-2026` |
| `Vanier` | `8KonRrq3avT5` | `https://www.simplebooth.com/gallery/8KonRrq3avT5-lge-fev-26` |
| `Brossard` | `urjGcth7fSdh` | `https://www.simplebooth.com/gallery/urjGcth7fSdh-lge-2026` |
| `Marché Central` | `3vaHgUNcJUos` | `https://www.simplebooth.com/gallery/3vaHgUNcJUos-lge-fev-2026` |
| `Lévis` | `ahebLG4GWjZF` | `https://www.simplebooth.com/gallery/ahebLG4GWjZF-lge-fev-2026` |
| `Sherbrooke` | `XCaTpPmn6r3h` | `https://www.simplebooth.com/gallery/XCaTpPmn6r3h-lge-fev-2026` |
| `Ste-Catherine` | `ALkJDisegsdU` | `https://www.simplebooth.com/gallery/ALkJDisegsdU-lge-fev-2026` |

### Fonctionnement du proxy backend (`/api/photobooth`)
Pour contourner les restrictions CORS du navigateur et alléger le traitement sur la TV, tout passe par la route `GET /api/photobooth` :
- **Paramètres** : `url` (URL ou code galerie), `limit=18` (par défaut 18 dernières photos), `order="asc"` (chronologique : de la plus ancienne parmi les N dernières jusqu'à la plus récente), `force=0` (`force=1` pour ignorer le cache).
- **Extraction du code** (`extract_gallery_code()`) : regex `/simplebooth\.com\/gallery\/([a-zA-Z0-9]+)/`.
- **Collecte paginée** (`_fetch_simplebooth_sync()`) :
  - Interroge `https://api.simplebooth.com/api/uploads?gallery={galleryCode}&order-by=created-at&order=desc&page-size=...`.
  - Suit `_links.next.href` (ou cursor `after`) jusqu'à obtenir `limit` photos valides (`deleted === false` et `processed === true`), dédupliquées par `upload.code`.
  - **Fallback HTML** (`_extract_window_simplebooth()`) : si les toutes premières entrées ne figurent que dans le state initial de la page `https://www.simplebooth.com/gallery/{galleryCode}`, lit l'objet injecté `window.simpleBooth`.
- **Normalisation** (`_normalize_upload()`) : expose `code`, `url` (`media.original`), `thumb` (`media.medium` `_m.jpg`), `small` (`media.thumbnail` `_sm.jpg`) et `createdAt` (`_parse_created_at()`).
- **Cache serveur** : `PHOTOBOOTH_CACHE_TTL = 90.0` secondes en mémoire.

### Affichage TV & Cookies de configuration
- **Bandeau continu pleine largeur (`#pb-section`)** : affiché uniquement en phase `LOBBY`, en bas d'écran.
- **Animation Marquee gauche → droite** : piste `.pb-marquee-track` avec animation `marqueeLeftToRight` (`translate3d(-50%,0,0)` → `translate3d(0,0,0)`), duplication fluide de la boucle de photos.
- **Rafraîchissement automatique** : toutes les **3 minutes** (`180000` ms) en arrière-plan pour capter les nouvelles photos du photobooth sans couper le quiz.
- **Contrôle en Régie** :
  - Bouton **`#btn-pb-toggle`** (`👁️` / `🙈`) pour masquer ou réafficher le bandeau instantanément.
  - Éditeur de galeries dans la régie (changement de succursale ou ajout d'un lieu personnalisé).
  - Sauvegarde locale sur l'appareil TV via cookies : `lge_pb_store`, `lge_pb_opts`, `lge_pb_stores`.

---

## 8. Optimisations de performance TV Samsung (Tizen)

Les navigateurs intégrés des téléviseurs Samsung Tizen disposent de CPU/GPU beaucoup plus modestes qu'un ordinateur et sont sensibles aux recalculs CSS/DOM :

| Optimisation appliquée | Problème résolu sur TV Samsung | Implémentation dans `static/index.html` |
| --- | --- | --- |
| **Mémoïsation DOM & `setText()`** | Le CDN Tailwind (`cdn.tailwindcss.com`) installe un `MutationObserver` : toute réécriture DOM chaque seconde relançait une compilation CSS et faisait lagger le chrono | Clés de cache `lastPlayersKey`, `lastCatsStateKey`, `lastDiffKey`, `pbTrackKey`, `regieCatsKey` + helper `setText()` ne touchant le DOM qu'en cas de changement réel |
| **Flux allégé & images `_m.jpg`** | Charger 50 images haute résolution en parallèle saturait la RAM/décodage vidéo Tizen et provoquait des saccades | Limitation à **18 dernières photos** (`limit=18`), chargement du format intermédiaire `_m.jpg` (`p.thumb \|\| p.url`), attribut `decoding="async"` |
| **Préchargement séquentiel** | Le préchargement parallèle de dizaines d'images bloquait le thread principal | Fonction `preloadPhotos` séquentielle (une image à la fois) |
| **Suppression `backdrop-blur` & bandeau texte** | Le flou d'arrière-plan CSS est très coûteux sur GPU Tizen et l'overlay texte surchargeait l'affichage | Retrait complet du bandeau texte `📸 Photobooth — ...` et de `backdrop-blur` sur le slideshow |
| **Barre de chrono GPU (`scaleX`)** | Animer `width` provoque des *layout reflows* continus | Barre `#bar` animée exclusivement via `transform: scaleX(...)` |
| **Isolation de rendu CSS** | Repaints en cascade entre le chrono, la liste de joueurs et le marquee | `contain: layout paint` sur `#timer, #pb-section, #player-list` + `backface-visibility: hidden` sur `.pb-marquee-track` + `translate3d` |
| **Suppression du `pulse-soft` sur `● LIVE`** | Une animation CSS infinie sur un badge en permanence à l'écran consommait des cycles de compositeur inutiles | Badge `● LIVE` rendu statique |
| **Anti-mise en veille** | Extinction automatique de la TV ou de l'appareil de contrôle | Utilisation de `navigator.wakeLock` |

---

## 9. Logging des KPI & Export automatique Google Sheets (Drive)

À la fin de chaque manche (entrée en `PODIUM`, ou retour anticipé au `LOBBY` si une partie active s'arrête), `Room.finalize_and_push_stats()` collecte les indicateurs de la partie et les pousse automatiquement via HTTPS `POST` vers le webhook Google Apps Script ([google_sheets_webhook.gs](google_sheets_webhook.gs)) lié au classeur cible (`GOOGLE_SHEET_ID = "1ZJ-sDjjC74DrQmhNJDXOargA7OH-4HJJ2ecenpQX0sI"`, `gid = 0`).

### Ordre exact des 6 colonnes enregistrées
1. **`Horodatage`** : date et heure locale Québec (`America/Toronto`, format `YYYY-MM-DD HH:MM:SS`).
2. **`Adresse IP de la session`** : adresse IP publique extraite des en-têtes proxy Render (`x-forwarded-for`, `x-real-ip` ou `websocket.client.host`).
3. **`Nombre de joueurs`** : total de joueurs distincts ayant participé à la manche.
4. **`Nombre de questions répondues par joueur`** : moyenne + détail individuel (ex. `13.5 moy. (Alice: 15/15, Bob: 12/15)`).
5. **`Temps de jeu par joueur`** : durée moyenne + détail individuel entre l'entrée et la sortie/fin (ex. `4m 05s moy. (Alice: 4m 30s, Bob: 3m 40s)`).
6. **`Combien de partages de stats`** : compteur cumulé des partages déclenchés en phase `PODIUM` (`SHARE` sur `X`, `FB`, `WA`, `Copier` ou partage natif).

### Mise à jour en temps réel (`upsert` par `game_id`) & Configuration
- Si un joueur clique sur un bouton de partage pendant les 60 s du `PODIUM`, le serveur appelle à nouveau `trigger_stats_push()` avec le même `game_id` : `doPost()` dans [google_sheets_webhook.gs](google_sheets_webhook.gs) utilise `LockService` et `PropertiesService` pour mettre à jour la ligne existante (colonne 6) sans créer de doublon.
- **Configuration du Webhook** :
  - **Recommandé sur Render (persistant)** : définir la variable d'environnement `GOOGLE_SHEETS_WEBHOOK_URL` avec l'URL `/exec` du déploiement Web App Apps Script.
  - **Depuis la Régie TV (`⚙️`)** : l'encart **📊 Stats Google Drive** permet d'ouvrir le Sheet, télécharger l'historique local (`/api/stats/csv`), enregistrer/tester l'URL Webhook (`SET_SHEETS_WEBHOOK` / `TEST_SHEETS_PUSH`) et consulter le statut du dernier envoi (`#sheets-status`).

---

## 10. Gestion des utilisateurs (`LGE` / `AdminLGE`) & Blocage de page

- **Compte utilisateur TV `LGE`** (mot de passe initial par défaut : `LGE2026`, surchargeable via `LGE_DEFAULT_PASSWORD`) :
  - Débloque l'écran d'accueil verrouillé (`#page-lock-overlay`) et lance la session TV/WebSocket (`startTVSession()`).
  - N'a pas accès au panneau Régie (`POST /api/auth/login` avec `target="regie"` renvoie HTTP `403`).
- **Compte administrateur Régie `AdminLGE`** (mot de passe temporaire initial : `Admin2026!`, surchargeable via `ADMIN_DEFAULT_PASSWORD`) :
  - Protège l'ouverture du panneau **Régie (`⚙️`)** via la modale `#admin-modal`.
  - **Changement obligatoire à la 1ʳᵉ connexion** (`must_change_password = True`) : renvoie un `change_token` temporaire et impose la saisie + confirmation d'un nouveau mot de passe (`POST /api/auth/change-password`) avant d'accéder à la Régie.
  - Permet également de modifier les mots de passe de `LGE` et `AdminLGE` depuis l'encart **🔐 Sécurité & Utilisateurs** de la Régie (`#pwd-manage-panel`), et débloque automatiquement la page TV.
- **Sessions permanentes sans limite de temps** :
  - Les jetons `qal_user_token` et `qal_admin_token` (`localStorage`) sont signés en HMAC-SHA256 (`_make_auth_token`) sans date d'expiration (`exp`) ni déconnexion automatique.
  - Mots de passe hachés en **PBKDF2-HMAC-SHA256** (100 000 itérations + sel aléatoire) dans `auth_config.json`.
- **Option de blocage de page en Régie (`#btn-page-lock`)** :
  - Activée par défaut (`page_lock_enabled = True`).
  - Basculable en direct depuis la Régie (`TOGGLE_PAGE_LOCK` ou `POST /api/auth/page-lock`) pour imposer ou désactiver le déblocage par `LGE` au chargement de la page TV.
- **Déblocage & Régie Mobile par QR Code (`/?tv_auth=CODE&target=page|regie`, réservé au staff)** :
  - Affiché uniquement sur `#page-lock-overlay` (`#lock-qr`), `#admin-login-box` (`#admin-qr`) et `#regie` (`#btn-mobile-regie-qr` → `#regie-inline-qr`), jamais pour les joueurs (`?room=XXXXX`).
  - L'employé scanne le QR code sur son cellulaire (`initStaffAuth()`), saisit et valide obligatoirement son mot de passe (`POST /api/auth/pair-login`, avec changement imposé à la 1ʳᵉ connexion `AdminLGE`).
  - La TV consomme le jeton via polling (`GET /api/auth/pair-status?consume=1`, TTL 10 min) pour se débloquer automatiquement. Si connecté en `AdminLGE`, le cellulaire bascule sur une interface complète **⚙️ Régie Mobile** connectée à `/ws/tv?room=XXXXX`.

---

## 11. Référence API (HTTP, WebSocket & Stockage client)

### Routes HTTP
- `GET /` — Sert [static/index.html](static/index.html)
- `GET /health` — Sonde de santé serveur (pour Render / monitoring)
- `GET /api/logo` — Proxy PNG en cache mémoire pour le logo Laser Game Evolution (garantit l'export `<canvas>` sans erreur CORS)
- `GET /api/photobooth?url=...&limit=18&order=asc&force=0` — Proxy JSON Simple Booth avec cache 90 s
- `GET /api/stats` — État de configuration Google Sheets et 50 dernières parties en mémoire (`GAME_LOGS_HISTORY`)
- `POST /api/stats/config` — Configure `webhook_url` dans `stats_config.json`
- `POST /api/stats/test` — Pousse une ligne de test vers le Google Sheet
- `GET /api/stats/csv` — Téléchargement CSV (UTF-8 BOM) des parties enregistrées en mémoire
- `GET /api/auth/status?user_token=...&admin_token=...` — Statut du blocage de page et validité des jetons permanents
- `POST /api/auth/login` — Authentifie `LGE` ou `AdminLGE` (retourne `token` permanent ou `change_token` si `must_change_password`)
- `POST /api/auth/change-password` — Valide le changement de mot de passe (1ʳᵉ connexion ou gestion admin)
- `POST /api/auth/page-lock` — Active/désactive `page_lock_enabled` (réservé à `AdminLGE`)
- `POST /api/auth/pair-create` — Crée une session QR temporaire (`AUTH_PAIR_TTL = 600 s`) liée à la salle
- `GET /api/auth/pair-status?code=...&consume=0|1` — Polling TV de validation QR (consomme le code à `consume=1`)
- `POST /api/auth/pair-login` — Valide identifiant + mot de passe depuis le cellulaire pour approuver la session QR (et ouvrir la Régie Mobile pour `AdminLGE`)

### WebSocket TV : `/ws/tv?room=XXXXX`
- **Serveur → TV** :
  - `{"type": "room", "room": "XXXXX"}` à la connexion
  - `{"type": "state", ...}` à chaque tick ou transition (inclut `phase`, `timer`, `duration`, `speed`, `cat_index`, `voting_enabled`, `available_categories`, `played_categories`, `active_difficulty`, `beginner_stats`, `players`, `question`, `counts`, `choice_players`, `round_events`, `sheets_webhook_set`, `sheets_url`, `last_push_status`, `page_lock_enabled`, etc.)
- **TV / Régie → Serveur** :
  - `{"type": "NEXT"}` — Force la transition vers l'étape suivante
  - `{"type": "TOGGLE_SPEED"}` — Bascule `speed` entre `1` et `5`
  - `{"type": "TOGGLE_VOTE"}` — Active/désactive `voting_enabled`
  - `{"type": "SELECT_CATEGORY", "category": "...", "index": ..., "start": true/false}` — Sélectionne une catégorie (et démarre éventuellement la partie)
  - `{"type": "START_NEXT_CATEGORY"}` — Enchaîne sur une nouvelle catégorie
  - `{"type": "KICK_PLAYER", "player_id": "..."}` — Expulse le joueur ciblé
  - `{"type": "SET_SHEETS_WEBHOOK", "webhook_url": "..."}` — Enregistre l'URL du Web App Google Apps Script
  - `{"type": "TEST_SHEETS_PUSH"}` — Déclenche un envoi de test vers Google Sheets
  - `{"type": "TOGGLE_PAGE_LOCK"}` — Active/désactive le blocage de la page TV pour `LGE`

### WebSocket Joueur : `/ws/player/{room_code}/{player_id}`
- **Serveur → Joueur** :
  - `{"type": "state", ...}` — État personnel et de manche (`player_state()`)
  - `{"type": "error", "message": "..."}` — Erreur de salle/validation
  - `{"type": "kicked", "reason": "..."}` — Notification d'exclusion (AFK 5 questions ou expulsion régie)
- **Joueur → Serveur** :
  - `{"type": "JOIN", "name": "...", "avatar": "...", "beginner": true/false, "vote_category": "..."}` (champ `category` également accepté)
  - `{"type": "VOTE", "beginner": true/false, "category": "..."}`
  - `{"type": "ANSWER", "choice": 0..3}`
  - `{"type": "SHARE", "channel": "native|x|facebook|whatsapp|copy"}` — Comptabilise un partage de score en phase `PODIUM` et met à jour le Google Sheet
  - `{"type": "LEAVE"}`

### Stockage local navigateur
- **`localStorage`** :
  - `qal_tv_room` — Code de la dernière salle TV créée/rejointe
  - `qal_user_token` — Jeton permanent de session utilisateur (`LGE`)
  - `qal_admin_token` — Jeton permanent de session Régie (`AdminLGE`)
  - `qal_player_id` — Identifiant unique persistant du joueur sur mobile
  - `qal_profile` — Profil mémorisé du joueur (pseudo, avatar, préférence débutant)
- **Cookies** :
  - `lge_pb_store` — Magasin Photobooth sélectionné sur la TV
  - `lge_pb_opts` — Options Photobooth (affichage/masquage, paramètres)
  - `lge_pb_stores` — Liste personnalisée des galeries Photobooth (incluant les éventuels ajouts depuis la régie)

---

## 12. Commandes d'exploitation & Déploiement (Local, Render, GitHub)

### Lancement en local
```powershell
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```
- Ouvrir l'écran TV : `http://localhost:8000/`
- Tester un mobile manuellement : `http://localhost:8000/?room=CODE5`

### Configuration Render
- **Build Command** : `pip install -r requirements.txt`
- **Start Command** : ``uvicorn server:app --host 0.0.0.0 --port $PORT``
- **Health Check Path** : `/health`
- **Variables d'environnement recommandées** :
  - `GOOGLE_SHEETS_WEBHOOK_URL` = URL `/exec` de la Web App déployée depuis [google_sheets_webhook.gs](google_sheets_webhook.gs)
  - Optionnelles : `LGE_DEFAULT_PASSWORD` (`LGE2026`), `ADMIN_DEFAULT_PASSWORD` (`Admin2026!`), `LGE_AUTH_SECRET`

### Synchronisation GitHub (`JoSurvie/quiz-arena-live`)
- La branche contenant toutes les évolutions (Photobooth, 300 questions, Gamification, Anti-AFK, Mix Aléatoire, Optimisations Samsung TV, Export KPI Google Sheets, Auth `LGE`/`AdminLGE`) est **`merge-photobooth`**.
- Vérifier avant tout déploiement Render que la branche déployée pointe bien sur `merge-photobooth` (ou fusionner `merge-photobooth` dans `main`).
