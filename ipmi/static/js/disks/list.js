function refreshDiskProperties() {
    var xhttp = new XMLHttpRequest();
    xhttp.onreadystatechange = function() {
        if (this.readyState == 4 && this.status == 200) {
            console.log(this.response);
        }
    }
    xhttp.open("GET", "/disk/refresh", true);
    xhttp.send();
};