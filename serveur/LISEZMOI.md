# Serveur en ligne de Solascii

Gère les comptes (identifiant « Pseudo#1234 », sans mot de passe), les amis, les salons
(publics, entre amis, privés avec code) et relaie le trafic des parties : les joueurs n'ont
aucun port à ouvrir, sur PC comme sur téléphone.

Fichiers : `serveur.py` (le serveur), `requirements.txt` (dépendances : `websockets`, `psycopg`).
Les comptes et listes d'amis sont enregistrés :
- dans une base PostgreSQL si la variable `DATABASE_URL` est définie (recommandé en ligne) ;
- sinon dans `comptes.json` (dossier choisi par `DATA_DIR`).

## Tester sur son PC

```bash
pip install -r requirements.txt
python serveur.py
```

Puis lancez le jeu avec `-- --server-url ws://127.0.0.1:8770`, ou mettez cette adresse dans
`project.godot` (`config/online_server_url`).

## Mettre en ligne (gratuit) : Render + Neon

Render héberge le serveur, Neon garde les comptes et les amis pour toujours (le disque de
Render est effacé à chaque redémarrage). Les deux sont gratuits, sans carte bancaire.

### 1. La base de données (Neon)
1. Créez un compte sur https://neon.tech puis un projet (région Europe, ex. Frankfurt).
2. Sur le tableau de bord, *Connect* : copiez la chaîne de connexion
   (`postgresql://utilisateur:motdepasse@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require`).
   Gardez-la secrète. La table est créée automatiquement au premier démarrage.

### 2. Le serveur (Render)
1. Créez un dépôt GitHub (privé, c'est possible) contenant `serveur.py` et `requirements.txt`.
2. Sur https://render.com : *New → Web Service*, choisissez ce dépôt.
   - Runtime : **Python 3**
   - Build Command : `pip install -r requirements.txt`
   - Start Command : `python serveur.py`
   - Instance Type : **Free**
   - *Environment* → ajoutez la variable `DATABASE_URL` = la chaîne copiée sur Neon.
3. Après le déploiement, le journal affiche « Comptes chargés depuis la base de données ».
   Render donne une adresse du type `https://solascii-serveur.onrender.com` : l'ouvrir dans
   un navigateur affiche « Solascii OK ».
4. Dans `project.godot`, mettez `config/online_server_url="wss://solascii-serveur.onrender.com"`
   (notez **wss://**), puis publiez une nouvelle version du jeu.

Limite de l'offre gratuite de Render : le serveur s'endort après 15 minutes sans joueur ; la
première connexion prend alors jusqu'à une minute. Les comptes et amis, eux, sont conservés.
