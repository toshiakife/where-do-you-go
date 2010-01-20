from os import environ
import constants
import oauth
import foursquare
from models import UserInfo, UserVenue
from google.appengine.ext import db
from google.appengine.api.labs import taskqueue
from google.appengine.api.urlfetch import DownloadError
#from django.utils import simplejson as json
from datetime import datetime, timedelta
import logging


def get_new_fs_for_userinfo(userinfo):
  oauth_token, oauth_secret = constants.get_oauth_strings()
  credentials = foursquare.OAuthCredentials(oauth_token, oauth_secret)
  user_token = oauth.OAuthToken(userinfo.token, userinfo.secret)
  credentials.set_access_token(user_token)
  return foursquare.Foursquare(credentials)

def fetch_and_store_checkins(userinfo, limit=50):
  num_added = 0
  try:
    fs = get_new_fs_for_userinfo(userinfo)
    history = fs.history(l=limit, sinceid=userinfo.last_checkin)

  # except DownloadError, err:
  #   logging.error("Checkins not fetched for %s with error %s" % (userinfo.user, err.args))
  #   #TODO i should maybe fail more gracefully here and try again?
  #   return num_added
  except foursquare.FoursquareRemoteException, err:
    logging.error("Checkins not fetched for %s with error %s" % (userinfo.user, err))
    #TODO i should maybe fail more gracefully here and try again?
    return num_added

  logging.debug(history)
  userinfo.is_authorized = True
  try:
    if not 'checkins' in history:
      if 'unauthorized' in history:
        userinfo.is_authorized = False
        logging.info("User %s is no longer authorized" % userinfo.user)
        return 0
      else:
        logging.warning("no value for 'checkins' or 'unauthorized' in history: " + str(history))
        return -1
    elif history['checkins'] == None:
      return 0

    userinfo.put()
    for checkin in history['checkins']:
      if 'venue' in checkin:
        j_venue = checkin['venue']
        if 'id' in j_venue and 'geolat' in j_venue and 'geolong' in j_venue:
          uservenue = UserVenue.all().filter('user =', userinfo.user).filter('venue_id =', j_venue['id']).get()
          if uservenue == None:
            uservenue = UserVenue(location = db.GeoPt(j_venue['geolat'], j_venue['geolong']))
            uservenue.update_location()
            uservenue.user = userinfo.user
            userinfo.venue_count = userinfo.venue_count + 1
            uservenue.venue_id       = int(j_venue['id'])
            if 'name' in j_venue:
              uservenue.name         = j_venue['name']
            try:
              if 'address' in j_venue:
                uservenue.address      = j_venue['address']
            except BadValueError:
              logging.error("Address not added for venue %s with address json '%s'" % (j_venue['id'], j_venue['address']))
            if 'cross_street' in j_venue:
              uservenue.cross_street = j_venue['cross_street']
            # if 'city' in j_venue:
            #   uservenue.city         = j_venue['city']
            if 'state' in j_venue:
              uservenue.state        = j_venue['state']
            if 'zip' in j_venue:
              uservenue.zipcode      = j_venue['zip']
            if 'phone' in j_venue:
              uservenue.phone        = j_venue['phone']
          uservenue.last_updated = datetime.strptime(checkin['created'], "%a, %d %b %y %H:%M:%S +0000") #WARNING last_updated is confusing and should be last_checkin_at
          if datetime.now() < uservenue.last_updated + timedelta(hours=12):  continue #WARNING last_updated is confusing and should be last_checkin_at
          uservenue.checkin_list.append(checkin['id'])
          uservenue.put()
          userinfo.checkin_count += 1
          if checkin['id'] > userinfo.last_checkin: userinfo.last_checkin = checkin['id'] # because the checkins are ordered with most recent first!
          userinfo.put()
          num_added += 1
      #   else: # there's nothing we can do without a venue id or a lat and a lng
      #     logging.info("Problematic j_venue: " + str(j_venue))
      # else:
      #   logging.info("No venue in checkin: " + str(checkin))
  except KeyError:
    logging.error("There was a KeyError when processing the response: " + content)
    raise
  return num_added

def fetch_and_store_checkins_initial(userinfo):
  #logging.info("about to fetch, userinfo.last_checkin = %d" % (userinfo.last_checkin))
  num_added = fetch_and_store_checkins(userinfo)
  #logging.info("%d checkins added this time, userinfo.last_checkin = %d" % (num_added, userinfo.last_checkin))
  if num_added != 0:
    #logging.info("more than 0 checkins added so there might be checkins remaining. requeue!")
    taskqueue.add(url='/fetch_foursquare_data/all_for_user/%s' % userinfo.key())
  else:
    #logging.info("no more checkins found, we're all set!")
    userinfo.is_ready = True
  userinfo.level_max = int(3 * constants.level_const)
  userinfo.put()

def fetch_and_store_checkins_for_batch():
  userinfos = UserInfo.all().order('last_updated').fetch(30)#.filter('is_authorized = ', True)
  logging.info("performing batch update for %d users-------------------------------" % len(userinfos))
  for userinfo in userinfos:
    if True:#userinfo.is_authorized:
      num_added = fetch_and_store_checkins(userinfo)
      logging.info("updating %d checkins for %s" % (num_added, userinfo.user) )
    else:
      logging.debug("did not update checkins for %s" % userinfo.user)
    userinfo.last_updated = datetime.now()
    userinfo.put()

def update_user_info(userinfo):
  fs = get_new_fs_for_userinfo(userinfo)
  user = fs.user()
  if 'user' in user:
    userinfo.real_name = user['user']['firstname']
    if 'photo' in user['user'] and not user['user']['photo'] == '' :
      userinfo.photo_url = user['user']['photo']
    else:
      userinfo.photo_url = constants.default_photo
    if 'checkin' in user['user'] and 'venue' in user['user']['checkin']:
      userinfo.citylat = user['user']['checkin']['venue']['geolat']
      userinfo.citylng = user['user']['checkin']['venue']['geolong']
    else:
      userinfo.citylat = constants.default_lat
      userinfo.citylng = constants.default_lng
    userinfo.put()
  else:
    logging.error('no "user" key in json: %s' % user)

if __name__ == '__main__':
  raw = environ['PATH_INFO']
  assert raw.count('/') == 2 or raw.count('/') == 3, "%d /'s" % raw.count('/')

  if raw.count('/') == 2:
    foo, bar, rest, = raw.split('/')
  elif raw.count('/') == 3:
    foo, bar, rest, userinfo_key = raw.split('/')

  if rest == 'update_users_batch':
    fetch_and_store_checkins_for_batch()
  elif rest == 'all_for_user':
    userinfo = db.get(userinfo_key)
    fetch_and_store_checkins_initial(userinfo)