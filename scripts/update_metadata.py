#!/usr/bin/env python3
import base64, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from mutagen import File
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, TPOS, APIC, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC, Picture
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus

REPO_ROOT = Path(__file__).resolve().parents[1]
SONGS_DIR = REPO_ROOT / 'Songs'
META_FILE = REPO_ROOT / 'metadata.json'
ART_DIR = REPO_ROOT / 'Artwork'
ACOUSTID_CLIENT = os.environ.get('ACOUSTID_CLIENT', '').strip()
USER_AGENT = 'VSMusicPlayer/2.0 (personal music library; GitHub Actions)'
AUDIO_EXTS = {'.mp3','.flac','.m4a','.mp4','.ogg','.opus','.wav','.aac'}
PROCESSOR_VERSION = '3.0'


def http_json(url, params=None, headers=None, delay=1.05):
    if delay:
        time.sleep(delay)
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params)
    h={'User-Agent':USER_AGENT,'Accept':'application/json'}
    if headers: h.update(headers)
    req=Request(url,headers=h)
    with urlopen(req,timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def http_bytes(url, delay=0.2):
    if delay: time.sleep(delay)
    req=Request(url,headers={'User-Agent':USER_AGENT})
    with urlopen(req,timeout=30) as r:
        return r.read(), r.headers.get_content_type()


def clean(v):
    if v is None: return ''
    if isinstance(v, list): return str(v[0]).strip() if v else ''
    return str(v).replace('\x00','').strip()


def first(v):
    return clean(v[0] if isinstance(v,list) and v else v)


def read_tags(path):
    f=File(path, easy=False)
    if f is None: return {}
    out={}
    tags=f.tags or {}
    def get(*keys):
        for k in keys:
            if k in tags:
                v=tags[k]
                if isinstance(v,list): v=v[0] if v else ''
                return clean(v)
        return ''
    if isinstance(f, MP4):
        out.update(title=get('©nam'),artist=get('©ART'),album=get('©alb'),albumArtist=get('aART'),genre=get('©gen'),year=get('©day'),track=get('trkn'),disc=get('disk'))
        if isinstance(tags.get('trkn'),list) and tags['trkn']:
            out['track']=str(tags['trkn'][0][0] or '')
        if isinstance(tags.get('disk'),list) and tags['disk']:
            out['disc']=str(tags['disk'][0][0] or '')
        if 'covr' in tags and tags['covr']:
            out['_cover_bytes']=bytes(tags['covr'][0])
            out['_cover_mime']='image/jpeg' if tags['covr'][0].imageformat==MP4Cover.FORMAT_JPEG else 'image/png'
    else:
        out.update(title=get('TIT2','title'),artist=get('TPE1','artist'),album=get('TALB','album'),albumArtist=get('TPE2','albumartist','album artist'),genre=get('TCON','genre'),year=get('TDRC','TYER','date','year'),track=get('TRCK','tracknumber'),disc=get('TPOS','discnumber'))
        # Easy Vorbis/FLAC keys may be lowercase.
        for k in ('title','artist','album','albumartist','genre','date','year','tracknumber','discnumber'):
            if not out.get(k) and k in tags:
                out[k if k not in ('albumartist','tracknumber','discnumber') else {'albumartist':'albumArtist','tracknumber':'track','discnumber':'disc'}[k]]=first(tags[k])
        for apic in tags.values():
            if hasattr(apic,'mime') and hasattr(apic,'data'):
                out['_cover_bytes']=apic.data; out['_cover_mime']=apic.mime; break
    return out


def score_tags(t):
    fields=['title','artist','album','genre','year']
    return sum(1 for x in fields if t.get(x)) / len(fields)


def fpcalc(path):
    p=subprocess.run(['fpcalc','-json',str(path)],capture_output=True,text=True,timeout=120)
    if p.returncode!=0: raise RuntimeError(p.stderr.strip() or 'fpcalc failed')
    data=json.loads(p.stdout)
    return int(round(float(data['duration']))), data['fingerprint']


def acoustid_lookup(duration, fingerprint):
    if not ACOUSTID_CLIENT: return None
    data=http_json('https://api.acoustid.org/v2/lookup',{
        'client':ACOUSTID_CLIENT,'duration':duration,'fingerprint':fingerprint,
        'meta':'recordings+recordingids+releases+releaseids+releasegroups+releasegroupids+tracks+compress'
    },delay=0.35)
    results=data.get('results') or []
    good=[r for r in results if r.get('score',0)>=0.85]
    if not good: return None
    best=max(good,key=lambda x:x.get('score',0))
    recordings=best.get('recordings') or []
    if not recordings: return None
    rec=recordings[0]
    artists=rec.get('artists') or []
    releases=rec.get('releases') or []
    return {
        'score':float(best.get('score',0)),
        'recording_id':rec.get('id',''),
        'title':rec.get('title',''),
        'artist':', '.join(a.get('name','') for a in artists if a.get('name')),
        'release_id':(releases[0].get('id') if releases else ''),
        'album':(releases[0].get('title') if releases else ''),
        'track':(releases[0].get('media',[{}])[0].get('track', '') if releases and releases[0].get('media') else ''),
    }


def musicbrainz_recording(mbid):
    if not mbid: return {}
    return http_json(f'https://musicbrainz.org/ws/2/recording/{mbid}',{'fmt':'json','inc':'releases+artists+artist-credits+genres'},delay=1.05)


def choose_release(releases):
    if not releases: return None
    # Prefer digital/official releases when available; otherwise first release with a date.
    def key(r):
        status=(r.get('status') or '').lower()
        date=r.get('date') or '9999'
        return (0 if status=='official' else 1, date, r.get('title',''))
    return sorted(releases,key=key)[0]


def cover_for_release(release_id):
    if not release_id: return None
    try:
        b,mime=http_bytes(f'https://coverartarchive.org/release/{release_id}/front-500',delay=0.15)
        return b,mime
    except Exception:
        return None


def write_tags(path, meta, cover=None):
    f=File(path, easy=False)
    if f is None: return False
    ext=path.suffix.lower()
    title=meta.get('title',''); artist=meta.get('artist',''); album=meta.get('album',''); album_artist=meta.get('albumArtist',''); genre=meta.get('genre',''); year=str(meta.get('year','') or '')[:4]; track=str(meta.get('track','') or ''); disc=str(meta.get('disc','') or '')
    try:
        if isinstance(f, MP4):
            if f.tags is None: f.add_tags()
            f['©nam']=[title] if title else []
            f['©ART']=[artist] if artist else []
            f['©alb']=[album] if album else []
            f['aART']=[album_artist] if album_artist else []
            f['©gen']=[genre] if genre else []
            f['©day']=[year] if year else []
            if track: f['trkn']=[(int(track.split('/')[0]),0)]
            if disc: f['disk']=[(int(disc.split('/')[0]),0)]
            if cover:
                data,mime=cover; fmt=MP4Cover.FORMAT_JPEG if 'jpeg' in mime or 'jpg' in mime else MP4Cover.FORMAT_PNG; f['covr']=[MP4Cover(data,imageformat=fmt)]
        elif isinstance(f, FLAC):
            if f.tags is None: f.add_tags()
            f['title']=[title] if title else []
            f['artist']=[artist] if artist else []
            f['album']=[album] if album else []
            f['albumartist']=[album_artist] if album_artist else []
            f['genre']=[genre] if genre else []
            f['date']=[year] if year else []
            if track: f['tracknumber']=[track]
            if disc: f['discnumber']=[disc]
            if cover:
                data,mime=cover; pic=Picture(); pic.data=data; pic.mime=mime; pic.type=3; f.clear_pictures(); f.add_picture(pic)
        elif isinstance(f, (OggVorbis,OggOpus)):
            if f.tags is None: f.add_tags()
            for k,v in [('title',title),('artist',artist),('album',album),('albumartist',album_artist),('genre',genre),('date',year),('tracknumber',track),('discnumber',disc)]:
                if v: f[k]=[v]
        else:
            # MP3/WAV: use ID3 frames.
            try: tags=ID3(path)
            except ID3NoHeaderError: tags=ID3()
            tags.delall('TIT2'); tags.delall('TPE1'); tags.delall('TPE2'); tags.delall('TALB'); tags.delall('TCON'); tags.delall('TDRC'); tags.delall('TRCK'); tags.delall('TPOS')
            if title: tags.add(TIT2(encoding=3,text=title))
            if artist: tags.add(TPE1(encoding=3,text=artist))
            if album_artist: tags.add(TPE2(encoding=3,text=album_artist))
            if album: tags.add(TALB(encoding=3,text=album))
            if genre: tags.add(TCON(encoding=3,text=genre))
            if year: tags.add(TDRC(encoding=3,text=year))
            if track: tags.add(TRCK(encoding=3,text=track))
            if disc: tags.add(TPOS(encoding=3,text=disc))
            if cover:
                data,mime=cover; tags.delall('APIC'); tags.add(APIC(encoding=3,mime=mime,type=3,desc='Cover',data=data))
            tags.save(path)
            return True
        f.save()
        return True
    except Exception as e:
        print(f'WARN: could not write tags for {path}: {e}')
        return False


def save_art(path, data, mime):
    if not data: return ''
    ART_DIR.mkdir(exist_ok=True)
    ext='.jpg' if 'jpeg' in mime or 'jpg' in mime else '.png'
    name=re.sub(r'[^A-Za-z0-9._-]+','_',str(path.relative_to(REPO_ROOT)).replace('/','_'))
    out=ART_DIR/(name+ext); out.write_bytes(data)
    return str(out.relative_to(REPO_ROOT)).replace('\\','/')


def file_sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as fh:
        for chunk in iter(lambda: fh.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def extract_artist_credits(mb, fallback=''):
    credits=[]
    for nc in (mb.get('artist-credit') or []):
        artist=nc.get('artist') if isinstance(nc,dict) else None
        if not isinstance(artist,dict):
            continue
        name=clean(nc.get('name') or artist.get('name'))
        mbid=clean(artist.get('id'))
        join=clean(nc.get('joinphrase'))
        if name and name.lower() not in {'chorus','remix','unknown','unknown artist','various artists','various artist'}:
            credits.append({'name':name,'mbid':mbid,'joinPhrase':join})
    if credits:
        return credits
    # Fallback for embedded/acoustid text when MusicBrainz artist-credit is unavailable.
    parts=re.split(r'\s+with\s+|\s*&\s*|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|,\s*', clean(fallback), flags=re.I)
    return [{'name':x.strip(),'mbid':'','joinPhrase':''} for x in parts if x.strip() and x.strip().lower() not in {'chorus','remix','unknown','unknown artist'}]


def artist_credit_text(credits, fallback=''):
    if not credits:
        return clean(fallback)
    out=''
    for i,c in enumerate(credits):
        out += c.get('name','')
        if i < len(credits)-1:
            out += c.get('joinPhrase') or ' & '
    return out.strip()


def clean_artist_text(value):
    v=clean(value)
    if v.lower() in {'chorus','remix','unknown','unknown artist','various artists','various artist'}:
        return ''
    return v

def process(path, metadata):
    rel=str(path.relative_to(REPO_ROOT)).replace('\\','/')
    original_sha=file_sha256(path)
    previous=metadata['songs'].get(rel)
    if previous and previous.get('processorVersion')==PROCESSOR_VERSION and previous.get('fileSha256')==original_sha:
        print('  unchanged - skipping')
        return

    tags=read_tags(path)
    meta={k:v for k,v in tags.items() if not k.startswith('_') and v}
    meta['artist']=clean_artist_text(meta.get('artist',''))
    source='embedded'
    confidence='medium' if score_tags(tags)>=0.8 else 'low'
    needs_review=score_tags(tags)<0.8
    mbid=''; release_id=''; score=0; artist_credits=[]
    cover=None

    # Prefer fingerprint identification. This is the authoritative correction path.
    if ACOUSTID_CLIENT:
        try:
            duration,fingerprint=fpcalc(path)
            hit=acoustid_lookup(duration,fingerprint)
            if hit:
                mbid=hit.get('recording_id',''); release_id=hit.get('release_id',''); score=float(hit.get('score',0) or 0)
                mb=musicbrainz_recording(mbid)
                releases=mb.get('releases') or []
                rel_release=choose_release(releases)
                artist_credits=extract_artist_credits(mb, hit.get('artist',''))

                meta['title']=mb.get('title') or hit.get('title') or meta.get('title','')
                credit_text=artist_credit_text(artist_credits, hit.get('artist',''))
                meta['artist']=clean_artist_text(credit_text) or clean_artist_text(hit.get('artist','')) or meta.get('artist','')
                # Preserve embedded album artist when available; otherwise use the first credited artist.
                meta['albumArtist']=clean_artist_text(meta.get('albumArtist','')) or (artist_credits[0]['name'] if artist_credits else meta.get('artist',''))
                if rel_release:
                    meta['album']=rel_release.get('title') or hit.get('album') or meta.get('album','')
                    meta['release_id']=rel_release.get('id','')
                    release_id=rel_release.get('id','') or release_id
                    # Use the earliest official release selected by choose_release.
                    meta['year']=(rel_release.get('date') or '')[:4]
                    if not meta.get('year'):
                        meta['year']=(mb.get('first-release-date') or '')[:4]
                    media=rel_release.get('media') or []
                    if media and media[0].get('tracks'):
                        # Find matching recording in the release media when possible.
                        for tr in media[0].get('tracks') or []:
                            if tr.get('recording',{}).get('id')==mbid:
                                pos=tr.get('position')
                                if pos: meta['track']=str(pos)
                                break
                    try:
                        cover=cover_for_release(release_id)
                        if cover:
                            meta['art']=save_art(path,*cover)
                            meta['_cover']=cover
                    except Exception as e:
                        print('WARN cover',release_id,e)

                # Use MusicBrainz genres when embedded genre is missing.
                if not meta.get('genre'):
                    genres=[clean(g.get('name')) for g in (mb.get('genres') or []) if isinstance(g,dict) and g.get('name')]
                    if genres: meta['genre']='; '.join(genres[:3])

                meta['musicbrainzRecordingId']=mbid
                meta['musicbrainzReleaseId']=release_id
                source='acoustid+musicbrainz'
                confidence='high' if score>=0.9 else 'medium'
                needs_review=score<0.9
        except Exception as e:
            print(f'WARN fingerprint {rel}: {e}')

    # If fingerprinting did not provide credits, derive separate artists from the embedded credit.
    if not artist_credits:
        artist_credits=extract_artist_credits({}, meta.get('artist',''))

    if not meta.get('title'): meta['title']=path.stem
    if not meta.get('artist'): meta['artist']='Unknown Artist'
    meta.setdefault('album','')
    meta.setdefault('albumArtist',meta.get('artist',''))
    meta.setdefault('genre','')
    meta.setdefault('year','')
    meta.setdefault('track','')
    meta.setdefault('disc','')

    # A year is considered valid only when it is a real 4-digit year.
    if not re.fullmatch(r'\d{4}', str(meta.get('year',''))[:4]):
        meta['year']=''
        needs_review=True

    cover=meta.pop('_cover',None)
    write_tags(path,meta,cover=cover)
    entry={
        'title':meta['title'],'artist':meta['artist'],'artistCredits':artist_credits,
        'album':meta.get('album',''),'albumArtist':meta.get('albumArtist',''),
        'genre':meta.get('genre',''),'year':str(meta.get('year',''))[:4],
        'track':str(meta.get('track','')),'disc':str(meta.get('disc','')),
        'art':meta.get('art',''),'source':source,'confidence':confidence,'needsReview':bool(needs_review),
        'musicbrainzRecordingId':meta.get('musicbrainzRecordingId',''),'musicbrainzReleaseId':meta.get('musicbrainzReleaseId',''),
        'processorVersion':PROCESSOR_VERSION,'fileSha256':file_sha256(path)
    }
    metadata['songs'][rel]=entry


def main():
    if not SONGS_DIR.exists():
        print('Songs folder not found'); sys.exit(1)
    metadata={'version':2,'updatedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'songs':{}}
    if META_FILE.exists():
        try:
            old=json.loads(META_FILE.read_text(encoding='utf-8'))
            metadata['songs']=old.get('songs',{})
        except Exception: pass
    files=sorted(p for p in SONGS_DIR.rglob('*') if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    print(f'Found {len(files)} audio files')
    for i,path in enumerate(files,1):
        print(f'[{i}/{len(files)}] {path.relative_to(REPO_ROOT)}')
        process(path,metadata)
    META_FILE.write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Updated metadata.json')

if __name__=='__main__': main()
