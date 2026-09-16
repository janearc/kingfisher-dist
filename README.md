# kingfisher

             ____,_@--*,_
          _@==-==---#@@@@@@,__
       _,#-========----@@@==-=@,_
      _@=============---@--=--@@@@_
    @@..:-===+===++++===--@--=-@=@@@*_
           ..::-===+++++=--==-=--=-=@*@_
               :===++==++++=====-=@@@+@@_
    ..         :-======++*++*+==+=@==@@@@*
    -..........:==------=+##o%%#*+===++@%@@
    ==-::----=+++===-====-=#%##*+*++==+=@+=
    """%-+++++++++++++++++==++**++#*++=++==_
    @@@@@++*+++++**+++++++++++++*****+++*+-@
    ++++=+*++++******++========++*+++*+++==-%_
    ****=*+++++++==-----::::....::-=+*****++*@%_
    ***+**++++++**++++-:::..........::-=+****+@/%,
    *+***++++***********-:@@@#@#@@@#####""%@+***@@=,_
    *********************"                  ^@++**++=,
    ******************@"                       ^%+***+@
    ****************#"                            "%***+@
    ************+@"                                  "%***_
    +*********#"              __   _             _____  "%*+__
    ++++++@#"                / /__(_)___  ____ _/ __(_)____^@%_  ___  _____
    +@#""                   / //_/ / __ \/ __ `/ /_/ / ___/ __ \/ _ \/ ___/
                           / ,< / / / / / /_/ / __/ (__  ) / / /  __/ /
                          /_/|_/_/_/ /_/\__, /_/ /_/____/_/ /_/\___/_/
                                       /____/

## name

kingfisher -- map data as a service: the catalogue, the index, the shelf

## synopsis

    ./serve.py --root <dir> --mount /<name>/=<dir> [--port N]
    ./ingestd.py          the spool to the shelf
    ./gibsd.py  ./weatherd.py  ./openskyd.py      on an interval

## description

a read-only mount table, so a viewer's page fetches map bytes from its
own origin. an index of what NOAA, USGS, Overture, NASA and Carto have
on their shelves, with a state per dataset. a shelf of what has been
ingested, served as directories.

every act that reaches somebody else's server is behind a flipr flag and
defaults off, so no deploy starts spending on its own. the contract is
protobuf, served at `/api`, and every rpc is counted by method and
outcome.

    /            what this instance serves      /metrics  prometheus
    /health      liveness                       /stats    counters
    /api         the descriptor set             /reload   re-read
    /discovery   the index, held or not         /<mount>/ a mount's index

## see also

DESIGN.md, USING.md, OPERATION.md, LICENSE.txt
