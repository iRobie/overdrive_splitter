#!/usr/bin/env python3
import argparse
import os
import subprocess
from shutil import copyfile
import sys
import datetime
import time
import xml.etree.ElementTree
import re
import logging
import glob
import csv
import pandas
from pathlib import Path

from clint.textui import colored, progress
import eyed3
from eyed3.utils import art
from mutagen.mp3 import MP3


import numpy
import json

logger = logging.getLogger(__file__)
ch = logging.StreamHandler()
ch.setLevel(logging.WARN)
logger.addHandler(ch)
logger.setLevel(logging.INFO)

#### OPTIONS ######
output_debug_csvs = True
title_include_track_number = False # True: 01/09 - Title; False: Title
folder_include_series = True # True: Series/Year - Album ; False: Year - Album
overwrite_mp3tags = True # Write mp3 tags, even if ffmpeg doesn't process the file
moveChapterToSilenceSeconds = 5 # If silence is found within this many seconds, move chapter mark to silence
###################

__version__ = '0.1.0'  

MARKER_TIMESTAMP_MMSS = r'(?P<min>[0-9]+):(?P<sec>[0-9]+)\.(?P<ms>[0-9]+)'
MARKER_TIMESTAMP_HHMMSS = r'(?P<hr>[0-9]+):(?P<min>[0-9]+):(?P<sec>[0-9]+)\.(?P<ms>[0-9]+)'


def hhmmss_to_seconds(marker_timestamp):
    """
    Convert HH MM SS (FFF) timestamps to number of seconds
    """
    timestamp = None
    for r in ('%M:%S.%f', '%H:%M:%S.%f'):
        try:
            timestamp = time.strptime(marker_timestamp, r)
            ts = datetime.timedelta(
                hours=timestamp.tm_hour, minutes=timestamp.tm_min, seconds=timestamp.tm_sec)
            ts_mark = int(ts.total_seconds())
            break
        except ValueError:
            pass

    if not timestamp:
        # some invalid timestamp string, e.g. 60:15.00
        mobj = re.match(MARKER_TIMESTAMP_HHMMSS, marker_timestamp)
        if mobj:
            ts_mark = int(mobj.group('hr')) * 60 * 60 + \
                        int(mobj.group('min')) * 60 + \
                        int(mobj.group('sec')) 
        else:
            mobj = re.match(MARKER_TIMESTAMP_MMSS, marker_timestamp)
            if mobj:
                ts_mark = int(mobj.group('min')) * 60 + \
                            int(mobj.group('sec'))
            else:
                raise ValueError('Invalid marker timestamp: {}'.format(marker_timestamp))
    return ts_mark

def convert_time(time_secs):
    """
    Convert seconds to HH MM SS (FFF)
    Offset is how many seconds to subtract from the original value
    """
    fraction = int((time_secs % 1) * 1000)
    seconds = int(time_secs)
    min, sec = divmod(seconds, 60)
    hour, min = divmod(min, 60)
    return f"{hour:02}:{min:02}:{sec:02}.{fraction:03}"


def closestSilence(times, chapter, threshold):   
    """
    Searching silence list for chapter mark, within threshold
    Returns a value if found
    Returns original marker if not found
    """

    logger.debug("Checking for closest silence to {}".format(chapter))
    closest = times[min(range(len(times)), key = lambda i: abs(times[i]-chapter))]
    logger.debug("Returning {}".format(closest))
    return closest

def mp3_duration_seconds(filename):
    # Ref: https://github.com/ping/odmpy/pull/3
    # returns the length of the mp3 in seconds

    # eyeD3's audio length function:
    # audiofile.info.time_secs
    # returns incorrect times due to it's header computation
    # mutagen does not have this issue
    audio = MP3(filename)
    return int(round(audio.info.length))

def getDirectories(base_directory, bookInfo):

    filepaths = {}
    filepaths['base_directory'] = base_directory
    filepaths['output_name'] = u'{} - {}'.format(str(bookInfo["year"]), make_safe_filename(bookInfo["title"])) ## Folder to store completed project. I set this as: BookTitle (Year)
    filepaths['output_path'] = u'{}/{}'.format(base_directory, filepaths['output_name']) ## Making the final folder stay as a subdir of processing directory
    if "series" in bookInfo:
        if folder_include_series:
            filepaths['output_path'] = u'{}/{}/{}'.format(base_directory, bookInfo["series"], filepaths['output_name']) ## Making the final folder stay as a subdir of processing directory
    filepaths['temp_path'] = u'{}/{}'.format(base_directory, 'temp') ## Making the final folder stay as a subdir of processing directory

    for key, value in filepaths.items():
        if 'path' in key:
            if not os.path.exists(value):
                os.makedirs(value)
    return filepaths

def checkKey(dict, key): 
    """
    Returns true if a dictionary's key is present and not None
    """
    if key in dict.keys(): 
        if dict[key] is None:
            return False
        return True
    return False

def make_safe_filename(s):
    """
    Returns string of a filesystem safe name
    """
    def safe_char(c):
        if c.isalnum():
            return c
        elif c == ' ' or c == '-' or c == '_':
            return c
        else:
            return ""
    return "".join(safe_char(c) for c in s).rstrip("")

def verifyTitle(base_directory):
    """
    Reads title/author from MP3, sets title, author, year
    """
    bookInfo = {}

    ## Get an MP3 from the directory

        ## Get all MP3s in current directory
    files = [f for f in glob.glob("{}/*.mp3".format(base_directory))]
    files.sort()

    filename = files[0]
    infoPath = Path("{}/bookinfo.json".format(base_directory))

    if infoPath.exists ():
        f = open(infoPath,)
        bookInfo = json.loads(f.read())

    else:
        audiofile = eyed3.load(filename)
        bookInfo["title"] = audiofile.tag.album
        bookInfo["author"] = audiofile.tag.artist
        bookInfo["path"] = str(base_directory)

        print("Read title as: {}".format(bookInfo["title"]))
        inputtitle = input("Press enter to accept, over enter new title: ")
        if len(inputtitle) > 1:
            bookInfo["title"] = inputtitle

        seriestitle = input("Series Name (press enter to skip):")
        if len(seriestitle) > 1:
            bookInfo["series"] = seriestitle
            bookNumber = input("Book number in series: ")
            bookInfo["booknumber"] = bookNumber.zfill(2)

        print("Read author as: {}".format(bookInfo["author"]))
        inputauthor = input("Press enter to accept, over enter new title: ")
        if len(inputauthor) > 1:
            bookInfo["author"] = inputauthor

        bookInfo["year"] = input("Book Year: ")

        bookInfo["safetitle"] = make_safe_filename(bookInfo["title"])
        with open(infoPath, "w") as f:
            json.dump(bookInfo, f)

    return bookInfo

def findsilence(part_filename):

    # Load your audio.
    silenceseconds = 1
    thresholdamp = 0.01

    dur = float(silenceseconds)
    thr = int(float(thresholdamp) * 65535)

    tmprate = 22050
    len2 = dur * tmprate
    buflen = int(len2     * 2)
    #            t * rate * 16 bits

    oarr = numpy.arange(1, dtype='int16')
    # just a dummy array for the first chunk

    command = [ 
            'ffmpeg', '-y',
            '-hide_banner',
            '-loglevel', 'info' if logger.level == logging.DEBUG else 'error', '-stats',
            '-i', part_filename,
            '-f', 's16le',
            '-acodec', 'pcm_s16le',
            '-ar', str(tmprate), # ouput sampling rate
            '-ac', '1', # '1' for mono
            '-']        # - output to stdout

    pipe = subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=10**8)

    tf = True
    pos = 0
    opos = 0
    part = 0

    silencearray = []

    while tf :

        raw = pipe.stdout.read(buflen)
        if raw == '' :
            tf = False
            break

        arr = numpy.fromstring(raw, dtype = "int16")

        rng = numpy.concatenate([oarr, arr])
        try:
            mx = numpy.amax(rng)
        except:
            break ## End of file
        if mx <= thr :
            # the peak in this range is less than the threshold value
            trng = (rng <= thr) * 1
            # effectively a pass filter with all samples <= thr set to 0 and > thr set to 1
            sm = numpy.sum(trng)
            # i.e. simply (naively) check how many 1's there were
            if sm >= len2 :
                part += 1
                apos = pos + dur * 0.5
                print(mx, sm, len2, apos)
                silencearray.append(opos)
                opos = apos

        pos += dur

        oarr = arr

    return silencearray

def build_chapter_object(part_filename, track_count, silenceparts):
    """
    Extracts Overdrive Markers from MP3 tag, stores them in list part_markers
    """
    audioparts = []
    audiofile = eyed3.load(part_filename)
    previous_chapter = "zycndkldms" ## Just a garbage value

    ## Read MP3 metadata
    for frame in audiofile.tag.frame_set.get(eyed3.id3.frames.USERTEXT_FID, []):
        if frame.description != 'OverDrive MediaMarkers':
            continue
        if frame.text:
            try:
                tree = xml.etree.ElementTree.fromstring(frame.text)
            except UnicodeEncodeError:
                tree = xml.etree.ElementTree.fromstring(frame.text.encode('ascii', 'ignore').decode('ascii'))

            ## Overdrive can do the following:
            ## * Store Chapter by itself: "Chapter 1"
            ## * Split Chapter within a single part file: "Chapter 1", "Chapter 1 (12:01)"
            ## * Split Chapter across multiple part files, with or without the (12:01) above
            ## * Also store as "Chapter 1 Part 1"

            ## chapter_section checks if chapter is split
            chapter_section = 0
            partPattern = re.compile("^(.*) (Part \d+|continued)")
            timestampPattern = re.compile("^(.*) (\(\d\d:\d\d\))") # Chapter 1 (12:01)
            for m in tree.iter('Marker'):
                marker_name = m.find('Name').text.strip()
                marker_timestamp = m.find('Time').text

                ## Store "Chapter 1" as previous timestamp; "Chapter 1 (12:01) starts with previous chapter"
                ## New chapter name (should just be "Chapter 1")
                if not marker_name.startswith(previous_chapter):
                    m = re.match(partPattern, marker_name)
                    t = re.match(timestampPattern, marker_name)
                    if m:
                        ## Chapter name "Chapter 1 Part 1"
                        previous_chapter = m.groups()[0].strip()
                        chapter_section = 0
                    elif t:
                        ## Chapter name "Chapter 1 (12:01) but didn't find previous chapter"
                        previous_chapter = t.groups()[0].strip()
                        chapter_section = 0
                    else:
                        ## Just "Chapter 1"
                        previous_chapter = marker_name
                        chapter_section = 0
                ## Chapter is continuing previous chapter - "Chapter 1 (12:01)" or "Chapter 1 Part 2"
                else:
                    chapter_section += 1

                # Save "Chapter 1" as the name, regardless of real name
                marker_name = previous_chapter
                ts_mark = hhmmss_to_seconds(marker_timestamp)
                
                start_time = ts_mark
                track_count += 1

                old_start_time = start_time
                closest_silence = closestSilence(silenceparts[make_safe_filename(part_filename)], ts_mark, moveChapterToSilenceSeconds)
                silence_found = False
                timedifference = abs(closest_silence-ts_mark)

                if timedifference < moveChapterToSilenceSeconds:
                    start_time = closest_silence
                    silence_found = True

                audiopart = {
                    'filename': part_filename, 
                    'file_number': track_count,
                    'chapter_name': marker_name,
                    'chapter_section': chapter_section,
                    'start_time': start_time,
                    'old_start_time': old_start_time,
                    'difference': timedifference,
                    'silence_found': silence_found,
                    'closest_silence': closest_silence,
                    'start_time_hhmmss': convert_time(start_time),
                    'closest_silence_hhmmss': convert_time(closest_silence)}
                audioparts.append(audiopart)
        break
    return audioparts, track_count

def smooshChapters(part_markers):
    """
    Combine chapter parts, get earliest start time and latest end time for each combined
    chapter part
    """

    smooshed_chapters = []

    df = pandas.DataFrame(part_markers).sort_values(by=['file_number'])
    ## Get all unique chapter names. "Chapter 1" in the above example, ignoring "Chapter 1 (12:01)"
    unique_chapters = df.chapter_name.unique()
    track_number = 0

    for chapter in unique_chapters:
        ## Get all parts that match this chapter
        chapters = df.loc[df['chapter_name'] == chapter]

        splitName = None
        track_number += 1

        ## Check if this chapter is spread across multiple MP3 files
        unique_parts = chapters.filename.unique()
        if len(unique_parts) > 1:

            # Save these files as a temporary Chapter-PartX for now
            for index, part in enumerate(unique_parts):
                parts = chapters.loc[chapters['filename'] == part]
                splitName = u"-Part" + str(index)

                ## If this chapter is split between MP3s, and we're not on the last chapter/part, endtime of (chapter/part) will be the endtime of the current MP3 files

                endtime = None
                if index < len(unique_parts) - 1:
                    endtime = convert_time(mp3_duration_seconds(parts['filename'].iloc[0]))

                smooshed_chapter = {
                    'filename': parts['filename'].iloc[0],
                    'track_number': track_number,
                    'chapter_name': parts['chapter_name'].iloc[0],
                    'split_name': parts['chapter_name'].iloc[0] + splitName,
                    'filenumber': parts.file_number.min(),
                    'start_time': convert_time(parts.start_time.min()),
                    'end_time': endtime,
                    'old_start_time': convert_time(parts.old_start_time.min()),
                    'silence_found': parts['silence_found'].iloc[0],
                    'closest_silence': convert_time(parts.closest_silence.min()),
                    'difference': parts['difference'].iloc[0]
                }
                smooshed_chapters.append(smooshed_chapter)
        else:
            smooshed_chapter = {
                'filename': chapters['filename'].iloc[0],
                'track_number': track_number,
                'chapter_name': chapters['chapter_name'].iloc[0],
                'split_name': None,
                'filenumber': chapters.file_number.min(),
                'start_time': convert_time(chapters.start_time.min()),
                'end_time': None,
                'old_start_time': convert_time(chapters.old_start_time.min()),
                'silence_found': chapters['silence_found'].iloc[0],
                'closest_silence': convert_time(chapters.closest_silence.min()),
                'difference': chapters['difference'].iloc[0]
            }
            smooshed_chapters.append(smooshed_chapter)

    ## Sort again, just in case
    smooshed_chapters = sorted(smooshed_chapters, key = lambda i: i['filenumber'])

    ## Get end time of this chapter
    for index, chapter in enumerate(smooshed_chapters):

        ## No endtime currently exists
        if not checkKey(chapter, 'end_time'):
            ## There are more files. End time is the start time of the next chapter
            if index < len(smooshed_chapters) - 1:
                endtime = smooshed_chapters[index+1]["start_time"]
            ## No more files. End time is the end of the file.
            else:
                endtime = convert_time(mp3_duration_seconds(chapter['filename']))

            ## It's possible the grabbed end time (start time of the next chapter) is 0, indicating a new MP3. In that case, the real end time is the end of the current file.
            if hhmmss_to_seconds(endtime) < 1:
                endtime = convert_time(mp3_duration_seconds(chapter['filename']))
            chapter['end_time'] = endtime

    return smooshed_chapters, track_number


def writeMP3Tags(mp3file, chaptertitle, tracknumber, bookInfo):
    """
    Write output Mp3 file with title, author, cover
    """

    audiofile = eyed3.load(mp3file)
    audiofile.tag.artist = bookInfo["author"]
    audiofile.tag.album = bookInfo["title"]
    audiofile.tag.album_artist = bookInfo["author"]
    audiofile.tag.title = chaptertitle
    audiofile.tag.track_num = tracknumber
    if "series" in bookInfo:
        # https://eyed3.readthedocs.io/en/latest/_modules/eyed3/id3/frames.html
        seriesFullName = "{}, Book {}".format(bookInfo["series"], bookInfo["booknumber"])
        audiofile.tag.frame_set.setTextFrame(b"TIT3",seriesFullName) # Subtitle, or Track/More
        audiofile.tag.frame_set.setTextFrame(b"TPOS",bookInfo["booknumber"]) # Disc number/part position
        audiofile.tag.frame_set.setTextFrame(b"TSOA",seriesFullName) # Album sort order
        #audiofile.tag.frame_set.setTextFrame(b"TSST",seriesFullName) # Set Subtitle
    audiofile.tag.recording_date = bookInfo["year"]
    audiofile.tag.images.set(
        art.TO_ID3_ART_TYPES[art.FRONT_COVER][0], bookInfo["cover_bytes"], 'image/jpeg', description=u'Cover')

    audiofile.tag.save()

def writeChapterFiles(chapters, totaltracks, filepaths, bookInfo):
    failed_files = []

    leadingzeros = 2
    if totaltracks > 99:
        leadingzeros = 3

    ## Store items that need reprocessing
    needsmerging = []
    for chapter in chapters:

        ## Set all the names.  
        outputname = u'{} - {}.mp3'.format(str(chapter['track_number']).zfill(leadingzeros), make_safe_filename(chapter['chapter_name'])) ## MP3 name. I set as Track - Title
        outputpath = u'{}/{}'.format(filepaths['output_path'], outputname) ## Tells ffmpeg this is the output for this part
        
        outputtitle =  u'{}'.format(chapter['chapter_name'])
        if title_include_track_number:
            outputtitle =  u'{}/{} - {}'.format(str(chapter['track_number']).zfill(leadingzeros), str(totaltracks).zfill(leadingzeros), chapter['chapter_name']) ## Set title at Track/Total Tracks - Chapter Name. My personal preference.
        mp3tracknumber = str(chapter['track_number']).zfill(leadingzeros)

        ## Check if this is a split chapter
        ### Store split chapters in temp directory and save in needsmerging list
        if checkKey(chapter, 'split_name'):
            tempoutputname = u'{} - {}.mp3'.format(str(chapter['track_number']).zfill(leadingzeros), make_safe_filename(chapter['split_name'])) ## MP3 name. I set as.\sp Track - Title
            tempoutputpath = u'{}/{}'.format(filepaths['temp_path'], tempoutputname)
            needsmerge = {
                'filenumber': chapter['filenumber'],
                'track_number': chapter['track_number'],
                'merge_input': tempoutputpath,
                'merge_output': outputpath,
                'outputtitle':  outputtitle,
                'mp3tracknumber': mp3tracknumber
            }
            needsmerging.append(needsmerge)
            outputpath = tempoutputpath
 
        writemp3tags = False
        if overwrite_mp3tags:
            writemp3tags = True
        if os.path.isfile(outputpath):
            logger.info('Already saved "{}"'.format(
                colored.magenta(outputpath)))
        else:
            cmd = [
                'ffmpeg', '-y',
                '-hide_banner',
                '-loglevel', 'info' if logger.level == logging.DEBUG else 'error', '-stats',
                '-i', chapter['filename'],
                '-acodec', 'copy',
                '-ss', chapter['start_time'],
                '-to', chapter['end_time'],
                outputpath]
            exit_code = subprocess.call(cmd)
            writemp3tags = True

            if exit_code > 0:
                print("Failed processing {}".format(outputpath))
                failed_files.append(chapter)
                continue

        ## Tag MP3 file
        if writemp3tags:
            writeMP3Tags(outputpath, outputtitle, mp3tracknumber, bookInfo)
        
    return needsmerging
        
def processMerges(chapters, bookInfo):
    failed_files = []

    ## Group by track number for safety
    df = pandas.DataFrame(chapters).sort_values(by=['filenumber'])
    track_numbers = df.track_number.unique()

    for track in track_numbers:
        tracks = df.loc[df['track_number'] == track]

        ## Send a list of files to concat, along with output name
        input = tracks.merge_input.tolist()
        output = tracks.merge_output.min()
        written, successful = mergemp3s(input, output)
        if written:
            writeMP3Tags(output, tracks.outputtitle.min(), tracks.mp3tracknumber.min(), bookInfo)

        if not successful:
            failed_files.append(track)

    return failed_files

def mergemp3s(files, output_filename):
    if os.path.isfile(output_filename):
        logger.info('Already saved "{}"'.format(
            colored.magenta(output_filename)))
        return False, True

    logger.info('Generating "{}"...'.format(
        colored.magenta(output_filename)))

    # We can't directly generate a m4b here even if specified because eyed3 doesn't support m4b/mp4
    cmd = [
        'ffmpeg', '-y',
        '-hide_banner',
        '-loglevel', 'info' if logger.level == logging.DEBUG else 'error', '-stats',
        '-i', 'concat:{}'.format('|'.join(files)),
        '-acodec', 'copy',
        output_filename ]
    exit_code = subprocess.call(cmd)
    if exit_code == 0:
        return True, True
    return False, False

def outputCSV(mydict, outname):
    with open(outname, 'w', newline='\n', encoding='utf-8') as f:
        fc = csv.DictWriter(f, 
                    fieldnames=mydict[0].keys(),
                    )
        fc.writeheader()
        fc.writerows(mydict)

def processDir(base_directory):

    silenceparts = {}
    track_count = 0
    part_markers = []
    filepaths = {}
    failed_files = []

    if not os.path.exists(base_directory):
        print("Invalid directory passed to processDir")
        sys.exit(0)

    ## Get all MP3s in current directory
    files = [f for f in glob.glob("{}/*.mp3".format(base_directory))]
    files.sort()

    if len(files) == 0:
        print("No Mp3 files found at that location")
        return

    ## Find cover art. Panic if not found currently.
    coverfile = [f for f in glob.glob("{}/*Cover.jpg".format(base_directory))]
    cover_filename = coverfile[0]

    ## Get title, author, year.
    bookInfo = verifyTitle(base_directory)

    with open(cover_filename, 'rb') as f:
        bookInfo["cover_bytes"] = f.read()

    ## Set and create file directories
    filepaths = getDirectories(base_directory, bookInfo)

    ## Copy image to output directory
    copyfile(cover_filename, "{}/{}".format(filepaths['output_path'], os.path.basename(cover_filename)))

    ## Look for an load a previous silence file. This saves time on reprocessing.
    silencefile = Path("{}/silenceparts.json".format(filepaths['temp_path'], ))
    if silencefile.exists ():
        f = open(silencefile,)
        silenceparts = json.loads(f.read())

    ## Scan MP3s for silence. This can take a while.
    print('{}'.format(colored.green("-------------------")))
    print('{}'.format(colored.green("Step 1/3: Scan MP3 for silence")))
    print('{}'.format(colored.green("-------------------")))

    start = time.time()
    i = 0
    total = len(files) + 1
    for filename in files:
        if checkKey(silenceparts, make_safe_filename(filename)):
            logger.info("Silence already exists for {}".format(filename))
            total = total - 1
        else:
            i += 1
            percentcomplete = i / total
            elapsedtime = time.time() - start
            eta = elapsedtime / percentcomplete
            remainingtime = convert_time(eta - elapsedtime)
            if i == 1:
                remainingtime = convert_time(50 * total) ## Takes roughly 50 seconds per file. Good enough for an estimate.
            print('{}'.format(colored.green("File {} of {}  --  Remaining time: {}").format(i, total, remainingtime)))
            silenceparts[make_safe_filename(filename)] = findsilence(filename) 

            with open(silencefile, "w") as f:
                json.dump(silenceparts, f)

    ## read embedded bookmark tags.
    for filename in files:
        audioparts, track_count = build_chapter_object(filename, track_count, silenceparts)
        for part in audioparts:
            part_markers.append(part)

    # if output_debug_csvs:
    #     outputCSV(part_markers, '{}/partmarkers.csv'.format(filepaths['temp_path']))

    ## Combine chapter timestamps to have 1 output per chapter
    cuttimestamps, totaltracks = smooshChapters(part_markers)

    if output_debug_csvs:
        outputCSV(cuttimestamps, '{}/cuttimestamps.csv'.format(filepaths['temp_path']))

    ## Split MP3s to chapters
    print('{}'.format(colored.green("-------------------")))
    print('{}'.format(colored.green("Step 2/3: Save MP3s to chapter files")))
    print('{}'.format(colored.green("-------------------")))

    #needsmerging = []
    needsmerging = writeChapterFiles(cuttimestamps, totaltracks, filepaths, bookInfo)

    ## If any chapter spanned MP3s, combine them
    print('{}'.format(colored.green("-------------------")))
    print('{}'.format(colored.green("Step 3/3: Merge MP3s")))
    print('{}'.format(colored.green("-------------------")))

    if len(needsmerging) > 0:
        if output_debug_csvs:
            outputCSV(needsmerging, '{}/needsmerging.csv'.format(filepaths['temp_path']))
        failed_files = processMerges(needsmerging, bookInfo)
    else:
        print("No files needed merging")

    ## TODO copy silence parts and cover.jpg to output dir

    return failed_files, cuttimestamps

def main():

    failures = {}
    books = []

    ## Setup the directory that this is working on
    parser = argparse.ArgumentParser( )

    parser.add_argument(
        '-d', '--dir', dest='dir', default='.')

    args = parser.parse_args()

    subfolders = [ f.path for f in os.scandir(".") if f.is_dir() ]

    for folder in subfolders:
        base_directory = folder.rstrip('\\')
        base_directory = folder.rstrip('"')

        base_directory = Path(folder)
        ## Get title, author, year. Save this for batch processing.
        books.append(verifyTitle(base_directory))

    ## Now that all input is gathered, run again
    for book in books:

        print('{}'.format(colored.blue("*******************")))
        print('{}'.format(colored.blue("Processing {}").format(book["title"])))
        print('{}'.format(colored.blue("*******************")))
        failed_files, cuttimestamps = processDir(book["path"])
        book["failed_files"] = failed_files
        book["cuttimestamps"] = cuttimestamps

    for book in books:    
        ## Write any failed files
        if len(book["failed_files"]) > 0:
            print("------")
            print("Following are failed files:")
            logger.error('{}'.format(
                colored.red("There were failures in this run.")))
            print('Error log Saved to FAILURES-{}.csv'.format(book["safetitle"]))
            outputCSV(failed_files, 'FAILURES-{}.csv'.format(book["safetitle"]))

            ## Check for any chapters that weren't matched
        for chapter in book["cuttimestamps"]:
            ## Get all parts that match this chapter
            if not chapter['silence_found']:
                logger.error('{}'.format(
                    colored.red("Some chapters were placed on a spot without silence, for {}. Review the cuttimestamps.csv file.").format(book["safetitle"])))
                break

if __name__ == "__main__":
    main()
