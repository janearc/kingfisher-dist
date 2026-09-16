# Failure kinds the ingest loop has to tell apart.
#
# WHY THIS EXISTS. ingestd's spool pass had exactly one failure path: log it and
# leave the file where it is. That is correct for a transient failure -- a disk
# hiccup, a half-written payload, a pipeline that has not been deployed yet --
# because the next pass retries and the work eventually lands.
#
# It is catastrophic for a failure that can never succeed. Measured 2026-09-01:
# 119 USGS tiles that declare no coordinate reference system were re-read and
# re-refused 4,463 times EACH in a single day. 531,097 failures, roughly 200MB
# of log volume, and not one item processed. The queue did continuous work and
# made no progress, because nothing could ever leave it.
#
# The distinction cannot be made by the loop. Matching on an error string is
# fragile and puts the knowledge in the wrong place. So the code that REFUSES
# declares whether its refusal is final, and the loop honours that.
#
# on why deleting the files is not the fix: "we can get rid of the files
# but it doesn't do us much good if we don't prevent it from reading them
# again." Exactly so -- kingfisher FETCHES these, so a deletion is a re-download
# waiting to happen. Dead-lettering moves them somewhere the scanner does not
# look while keeping them on disk, so the evidence survives and a reappearance
# is visible as a new arrival rather than an invisible loop.


class Unprocessable(ValueError):
    """This item can never succeed, however many times it is tried.

    Raise this ONLY when retrying is certainly pointless -- the data itself is
    unusable, not the attempt. A missing coordinate reference system qualifies:
    no number of passes will add one. A network timeout does not; nor does a
    pipeline that has not shipped yet, which the loop already handles by leaving
    the item to wait.

    Getting this wrong in the cautious direction costs a retry. Getting it wrong
    in the confident direction silently discards work, so when unsure, do not
    raise it -- a poison queue is loud and recoverable, a wrongly dead-lettered
    dataset is neither.

    Subclasses ValueError deliberately. These refusals ARE value errors -- the
    data is unusable -- and every existing `except ValueError` keeps catching
    them, so adding permanence to a refusal is not a breaking change to anything
    that already handled it. The extra type only lets a caller that cares about
    the distinction see it.
    """
