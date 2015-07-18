# -*- coding: utf-8 -*-
# Natural Language Toolkit: Twitter client
#
# Copyright (C) 2001-2015 NLTK Project
# Author: Ewan Klein <ewan@inf.ed.ac.uk>
#         Lorenzo Rubio <lrnzcig@gmail.com>
# URL: <http://nltk.org/>
# For license information, see LICENSE.TXT


"""
NLTK Twitter client

This module offers methods for collecting and processing Tweets. Most of the
functionality depends on access to the Twitter APIs, and this is handled via
the third party Twython library.

If one of the methods below returns an integer, it is probably a `Twitter
error code <https://dev.twitter.com/overview/api/response-codes>`_. For
example, the response of '420' means that you have reached the limit of the
requests you can currently make to the Twitter API. Currently, `rate limits
for the search API <https://dev.twitter.com/rest/public/rate-limiting>`_ are
divided into 15 minute windows.
"""

import datetime
import itertools
import json
import os
import requests
import time
import gzip
from nltk.compat import UTC


try:
    from twython import Twython, TwythonStreamer
    from twython.exceptions import TwythonRateLimitError, TwythonError
except ImportError as err:
    import textwrap
    MSG = """The NLTK twitterclient module requires the Twython package. See\
    https://twython.readthedocs.org/ for installation instructions."""
    err.msg = textwrap.fill(MSG)
    raise

from nltk.twitter.util import credsfromfile, guess_path
from nltk.twitter.api import TweetHandlerI, BasicTweetHandler



class Streamer(TwythonStreamer):
    """
    Retrieve data from the Twitter Streaming API.

    The streaming API requires
    `OAuth 1.0 <http://en.wikipedia.org/wiki/OAuth>`_ authentication.
    """
    def __init__(self, app_key, app_secret, oauth_token, oauth_token_secret):

        self.handler = None
        self.do_continue = True
        TwythonStreamer.__init__(self, app_key, app_secret, oauth_token,
                                 oauth_token_secret)

    def register(self, handler):
        """
        Register a method for handling Tweets.

        :param TweetHandlerI handler: method for viewing
        """
        self.handler = handler

    def on_success(self, data):
        """
        :param data: response from Twitter API
        """
        if self.do_continue:
            if self.handler is not None:
                if 'text' in data:
                    self.handler.counter += 1
                    self.handler.handle(data)
                    self.do_continue = self.handler.do_continue()
            else:
                raise ValueError("No data handler has been registered.")
        else:
            self.disconnect()
            self.handler.on_finish()


    def on_error(self, status_code, data):
        """
        :param status_code: The status code returned by the Twitter API
        :param data: The response from Twitter API

        """
        print(status_code)

    def sample(self):
        """
        Wrapper for 'statuses / sample' API call
        """
        while self.do_continue:

            # Stream in an endless loop until limit is reached. See twython
            # issue 288: https://github.com/ryanmcgrath/twython/issues/288
            # colditzjb commented on 9 Dec 2014

            try:
                self.statuses.sample()
            except requests.exceptions.ChunkedEncodingError as e:
                if e is not None:
                    print("Error (stream will continue): {0}".format(e))
                continue

    def filter(self, track='', follow='', lang='en'):
        """
        Wrapper for 'statuses / filter' API call
        """
        while self.do_continue:
            #Stream in an endless loop until limit is reached

            try:
                if track == '' and follow == '':
                    msg = "Please supply a value for 'track', 'follow'"
                    raise ValueError(msg)
                self.statuses.filter(track=track, follow=follow, lang=lang)
            except requests.exceptions.ChunkedEncodingError as e:
                if e is not None:
                    print("Error (stream will continue): {0}".format(e))
                continue


class Query(Twython):
    """
    Retrieve data from the Twitter REST API.
    """
    def __init__(self, app_key, app_secret, oauth_token,
                 oauth_token_secret):
        self.handler = None
        self.do_continue = True
        Twython.__init__(self, app_key, app_secret, oauth_token, oauth_token_secret)

    def register(self, handler):
        """
        Register a method for handling Tweets.

        :param TweetHandlerI handler: method for viewing or writing Tweets to a file.
        """
        self.handler = handler

    def expand_tweetids(self, ids_f, verbose=True):
        """
        Given a file object containing a list of Tweet IDs, fetch the
        corresponding full Tweets from the Twitter API.

        The API call `statuses/lookup` will fail to retrieve a Tweet if the
        user has deleted it.

        This call to the Twitter API is rate-limited. See
        <https://dev.twitter.com/rest/reference/get/statuses/lookup> for details.

        :param ids_f: input file object consisting of Tweet IDs, one to a line
        :return: iterable of Tweet objects in JSON format
        """
        ids = [line.strip() for line in ids_f if line]

        if verbose:
            print("Counted {0} Tweet IDs in {1}.".format(len(ids), ids_f))

        # The Twitter endpoint takes lists of up to 100 ids, so we chunk the
        # ids.
        id_chunks = [ids[i:i+100] for i in range(0, len(ids), 100)]

        chunked_tweets = (self.lookup_status(id=chunk) for chunk in
                          id_chunks)

        return itertools.chain.from_iterable(chunked_tweets)



    def _search_tweets(self, keywords, limit=100, lang='en'):
        """
        Assumes that the handler has been informed. Fetches Tweets from
        search_tweets generator output and passses them to handler

        :param str keywords: A list of query terms to search for, written as\
        a comma-separated string.
        :param int limit: Number of Tweets to process
        :param str lang: language
        """
        while True:
            if isinstance(self.handler, TweetWriter):
                max_id = self.handler.max_id
            else:
                max_id = None
            tweets = self.search_tweets(keywords=keywords, limit=limit, lang=lang,
                                        max_id=max_id)
            for tweet in tweets:
                self.handler.handle(tweet)
            if not (self.handler.do_continue() and self.handler.repeat):
                break
        self.handler.on_finish()

    def search_tweets(self, keywords, limit=100, lang='en', max_id=None,
                      retries_after_twython_exception=0):
        """
        Call the REST API ``'search/tweets'`` endpoint with some plausible
        defaults. See `the Twitter search documentation
        <https://dev.twitter.com/rest/public/search>`_ for more information
        about admissable search parameters.

        :param str keywords: A list of query terms to search for, written as\
        a comma-separated string
        :param int limit: Number of Tweets to process
        :param str lang: language
        :param int max_id: id of the last tweet fetched
        :param int retries_after_twython_exception: number of retries when\
        searching Tweets before raising an exception
        :rtype: python generator
        """
        if not self.handler:
            # if no handler is provided, `BasicTweetHandler` provides minimum
            # functionality for limiting the number of Tweets retrieved
            self.handler = BasicTweetHandler(limit=limit)

        count_from_query = 0
        if not max_id:
            results = self.search(q=keywords, count=min(100, limit), lang=lang,
                                  result_type='recent')
            count = len(results['statuses'])
            if count == 0:
                print("No Tweets available through rest api for those keywords")
                return
            count_from_query = count
            max_id = results['statuses'][count - 1]['id'] - 1

            for result in results['statuses']:
                yield result
                self.handler.counter += 1
                if self.handler.do_continue() == False:
                    return


        # Pagination loop: keep fetching Tweets until the desired count is
        # reached while dealing with Twitter rate limits.
        retries = 0
        while count_from_query < limit:
            try:
                mcount = min(100, limit-count_from_query)
                results = self.search(q=keywords, count=mcount, lang=lang,
                                      max_id=max_id, result_type='recent')
            except TwythonRateLimitError as e:
                print("Waiting for 15 minutes -{0}".format(e))
                time.sleep(15*60) # wait 15 minutes
                continue
            except TwythonError as e:
                print("Fatal error in Twython request -{0}".format(e))
                if retries_after_twython_exception == retries:
                    raise e
                retries += 1

            count = len(results['statuses'])
            if count == 0:
                print("No more Tweets available through rest api")
                return
            count_from_query += count
            # the max_id is also present in the Tweet metadata
            # results['search_metadata']['next_results'], but as part of a
            # query and difficult to fetch. This is doing the equivalent
            # (last tweet id minus one)
            max_id = results['statuses'][count - 1]['id'] - 1
            self.handler.max_id = max_id

            for result in results['statuses']:
                yield result
                self.handler.counter += 1
                if self.handler.do_continue() == False:
                    return

    def user_info_from_id(self, userids):
        """
        Convert a list of userIDs into a variety of information about the users.

        See <https://dev.twitter.com/rest/reference/get/users/show>.

        :param list userids: A list of integer strings corresponding to Twitter userIDs
        :rtype: list(json)
        """
        return [self.show_user(user_id=userid) for userid in userids]

    def user_tweets(self, screen_name, limit, include_rts='false'):
        """
        Return a collection of the most recent Tweets posted by the user

        :param str user: The user's screen name; the initial '@' symbol\
        should be omitted
        :param int limit: The number of Tweets to recover; 200 is the maximum allowed
        :param str include_rts: Whether to include statuses which have been\
        retweeted by the user; possible values are 'true' and 'false'
        """
        data = self.get_user_timeline(screen_name=screen_name, count=limit,
                                      include_rts=include_rts)
        self.handler.handle(data)




class Twitter(object):
    """
    Wrapper class with restricted functionality and fewer options.
    """
    def __init__(self):
        self._oauth = credsfromfile()
        self.streamer = Streamer(**self._oauth)
        self.query = Query(**self._oauth)


    def tweets(self, keywords='', follow='', to_screen=True, stream=True,
               limit=100, date_limit=None, lang='en', repeat=False,
               gzip_compress=False):
        """
        Process some Tweets in a simple manner.

        :param str keywords: Keywords to use for searching or filtering
        :param list follow: UserIDs to use for filtering Tweets from the public stream
        :param bool to_screen: If `True`, display the tweet texts on the screen,\
        otherwise print to a file

        :param bool stream: If `True`, use the live public stream,\
        otherwise search past public Tweets

        :param int limit: Number of Tweets to process
        :param tuple date_limit: The date at which to stop collecting new\
        data. This should be entered as a tuple which can serve as the\
        argument to `datetime.datetime`. E.g. `data_limit=(2015, 4, 1, 12,\
        40)` for 12:30 pm on April 1 2015.\
        Note that, in the case of streaming, it is the maximum date, i.e.\
        a date in the future; if not, it is the minimum date, i.e. a date\
        in the past

        :param str lang: language

        :param bool repeat: flag to determine whether multiple files should be\
        written. If `True`, the length of each file will be set by the value\
        of `limit`. Use only if `to_screen` is `False`. See also :py:func:`handle`.

        :param gzip_compress: if `True`, ouput files are compressed with gzip
        """
        if to_screen:
            handler = TweetViewer(limit=limit, date_limit=date_limit)
        else:
            handler = TweetWriter(limit=limit, date_limit=date_limit,
                                  stream=stream, repeat=repeat,
                                  gzip_compress=gzip_compress)

        if stream:
            self.streamer.register(handler)
            if keywords == '' and follow == '':
                self.streamer.sample()
            else:
                self.streamer.filter(track=keywords, follow=follow, lang=lang)
        else:
            self.query.register(handler)
            if keywords == '':
                raise ValueError("Please supply at least one keyword to search for.")
            else:
                self.query._search_tweets(keywords, limit=limit, lang=lang)



class TweetViewer(TweetHandlerI):
    """
    Handle data by sending it to the terminal.
    """

    def handle(self, data):
        """
        Direct data to `sys.stdout`

        :return: return ``False`` if processing should cease, otherwise return ``True``.
        :rtype: bool
        :param data: Tweet object returned by Twitter API
        """
        text = data['text']
        print(text)
        self.counter += 1

    def on_finish(self):
        print('Written {0} Tweets'.format(self.counter))


class TweetWriter(TweetHandlerI):
    """
    Handle data by writing it to a file.
    """
    def __init__(self, limit=2000, date_limit=None, stream=True,
                 fprefix='tweets', subdir='twitter-files', repeat=False,
                 gzip_compress=False):
        """
        :param int limit: number of data items to process in the current\
        round of processing

        :param bool stream: If `True`, use the live public stream,\
        otherwise search past public Tweets

        :param str fprefix: The prefix to use in creating file names for Tweet\
        collections

        :param str subdir: The name of the directory where Tweet collection\
        files should be stored

        :param bool repeat: flag to determine whether multiple files should be\
        written. If `True`, the length of each file will be set by the value\
        of `limit`. See also :py:func:`handle`.

        :param gzip_compress: if `True`, ouput files are compressed with gzip
        """
        self.fprefix = fprefix
        self.subdir = guess_path(subdir)
        self.gzip_compress = gzip_compress
        self.fname = self.timestamped_file()
        self.stream = stream
        self.repeat = repeat
        # max_id stores the id of the older tweet fetched
        self.max_id = None
        self.output = None
        TweetHandlerI.__init__(self, limit, date_limit)


    def timestamped_file(self):
        """
        :return: timestamped file name
        :rtype: str
        """
        subdir = self.subdir
        fprefix = self.fprefix
        if subdir:
            if not os.path.exists(subdir):
                os.mkdir(subdir)

        fname = os.path.join(subdir, fprefix)
        fmt = '%Y%m%d-%H%M%S'
        timestamp = datetime.datetime.now().strftime(fmt)
        if self.gzip_compress:
            suffix = '.gz'
        else:
            suffix = ''
        outfile = '{0}.{1}.json{2}'.format(fname, timestamp, suffix)
        return outfile


    def handle(self, data):
        """
        Write Twitter data as line-delimited JSON into one or more files.

        :return: return `False` if processing should cease, otherwise return `True`.
        :param data: tweet object returned by Twitter API
        """
        if self.startingup:
            if self.gzip_compress:
                self.output = gzip.open(self.fname, 'w')
            else:
                self.output = open(self.fname, 'w')
            print('Writing to {0}'.format(self.fname))

        json_data = json.dumps(data)
        if self.gzip_compress:
            self.output.write((json_data + "\n").encode('utf-8'))
        else:
            self.output.write(json_data + "\n")

        if self.date_limit:
            tweet_date = datetime.datetime.strptime(data['created_at'], '%a %b %d\
            %H:%M:%S +0000 %Y').replace(tzinfo=UTC)
            if (tweet_date > self.date_limit and self.stream == True) or \
               (tweet_date < self.date_limit and self.stream == False):
                if self.stream:
                    message = "earlier"
                else:
                    message = "later"
                print("Date limit {0} is {1} than date of current tweet {2}".\
                      format(self.date_limit, message, tweet_date))
                self.do_stop = True
                return

        self.startingup = False

    def on_finish(self):
        print('Written {0} Tweets'.format(self.counter))
        if self.output:
            self.output.close()

    def do_continue(self):
        if self.repeat == False:
            return TweetHandlerI.do_continue(self)

        if self.do_stop:
            # stop for a functional cause (e.g. date limit)
            return False

        if self.counter == self.limit:
            # repeat is True, thus close output file and
            # create a new one
            self._restart_file()
        return True


    def _restart_file(self):
        self.on_finish()
        self.fname = self.timestamped_file()
        self.startingup = True
        self.counter = 0